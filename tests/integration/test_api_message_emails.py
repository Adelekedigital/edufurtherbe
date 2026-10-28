"""The message emails end to end (Messages PRD, decision 303).

Each send queues the unread email's QStash job 15 minutes later; the job is the
signed callback, delivered here by hand at that moment. A send inside a
session's window is emailed at once instead. What reaches the provider is read
from the messages when the email is sent, so a read or deleted message is never
emailed. The fakes stand on `app.state.scheduler` and `app.state.notifier`, the
seams the code reads.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.message_helpers import (
    ROOT,
    Person,
    book,
    close_account,
    pair,
    person,
    scalar,
    send,
    thread,
)

from app.domain.message_emails import UNREAD_EMAIL_DELAY, held_until
from app.domain.messages import MessageContext
from app.domain.notifications import Channel, Notification
from app.infra.clients.scheduler import SchedulerError
from app.infra.db.message_emails import queue_unread_email
from app.infra.db.outbox import drain, enqueue

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

PATH = "/api/v1/callbacks/message-emails"
BASE = "https://api.example.test"
APP = "https://app.example.test"
SIGNING_KEY = "not-a-real-signing-key-for-local-tests"
SETTINGS = f"{ROOT}/settings"
LONG = "word " * 60


@dataclass
class FakeScheduler:
    published: list[dict[str, Any]] = field(default_factory=list)
    fails: bool = False

    def schedule(self, *, url: str, body: dict[str, Any], at: dt.datetime) -> None:
        if self.fails:
            raise SchedulerError("qstash said no")
        self.published.append({"url": url, "body": body, "at": at})


@dataclass
class Recorder:
    """The provider: what would have been sent, and to whom."""

    sent: list[tuple[Notification, str, MessageContext]] = field(default_factory=list)

    def send(
        self,
        *,
        notification: Notification,
        channel: Channel,
        to: str,
        context: MessageContext,
        idempotency_key: str,
    ) -> None:
        del channel, idempotency_key
        self.sent.append((notification, to, context))


@dataclass
class World:
    client: httpx.AsyncClient
    engine: AsyncEngine
    scheduler: FakeScheduler
    notifier: Recorder


@pytest.fixture
def world(db_engine: AsyncEngine, api_client: httpx.AsyncClient) -> World:
    app = api_client._transport.app  # type: ignore[attr-defined]
    scheduler, notifier = FakeScheduler(), Recorder()
    app.state.scheduler = scheduler
    app.state.notifier = notifier
    app.state.settings = app.state.settings.model_copy(
        update={
            "public_base_url": BASE,
            "app_base_url": APP,
            "qstash_current_signing_key": SecretStr(SIGNING_KEY),
        }
    )
    return World(api_client, db_engine, scheduler, notifier)


def signed(body: bytes, *, key: str = SIGNING_KEY) -> dict[str, str]:
    now = dt.datetime.now(dt.UTC)
    token = jwt.encode(
        {
            "iss": "Upstash",
            "sub": f"{BASE}{PATH}",
            "exp": int((now + dt.timedelta(minutes=5)).timestamp()),
            "nbf": int(now.timestamp()),
            "body": base64.urlsafe_b64encode(hashlib.sha256(body).digest()).decode(),
        },
        key,
        algorithm="HS256",
    )
    return {"Upstash-Signature": token, "Content-Type": "application/json"}


async def fire(world: World, recipient: Person | UUID) -> httpx.Response:
    """Deliver the unread email job, as QStash would 15 minutes after a send."""
    target = recipient.id if isinstance(recipient, Person) else recipient
    body = json.dumps({"recipient_id": str(target)}).encode()
    return await world.client.post(PATH, content=body, headers=signed(body))


async def said(world: World, sender: Person, to: Person, body: str = "Hello") -> dict[str, Any]:
    response = await send(world.client, sender, to, body)
    assert response.status_code == 201, response.text
    return dict(response.json())


async def rows(engine: AsyncEngine) -> list[tuple[str, str]]:
    async with engine.connect() as conn:
        found = await conn.execute(
            text(
                "SELECT event_type, status FROM outbox_events "
                "WHERE event_type LIKE 'message_%' ORDER BY created_at"
            )
        )
        return [(str(a), str(b)) for a, b in found]


async def emailed(engine: AsyncEngine, message_id: str) -> Any:
    return await scalar(engine, "SELECT emailed_at FROM messages WHERE id = :m", m=message_id)


async def read_up_to(world: World, reader: Person, other: Person, message_id: str) -> None:
    response = await world.client.post(
        f"{ROOT}/{other.id}/read", json={"up_to": message_id}, headers=reader.headers
    )
    assert response.status_code == 204, response.text


# --------------------------------------------------------------------------
# The send queues the job
# --------------------------------------------------------------------------


async def test_a_send_queues_the_unread_job_15_minutes_later(world: World) -> None:
    mentor, mentee = await pair(world.engine, "queue")
    client_id = uuid4()
    first = await send(world.client, mentee, mentor, "Hi", client_id=client_id)
    again = await send(world.client, mentee, mentor, "Hi", client_id=client_id)

    assert (first.status_code, again.status_code) == (201, 200)
    sent_at = dt.datetime.fromisoformat(first.json()["sent_at"])
    # One job, and none for the replay.
    assert world.scheduler.published == [
        {
            "url": f"{BASE}{PATH}",
            "body": {"recipient_id": str(mentor.id)},
            "at": sent_at + UNREAD_EMAIL_DELAY,
        }
    ]
    assert await rows(world.engine) == []


async def test_a_job_that_cannot_be_queued_never_fails_the_send(world: World) -> None:
    mentor, mentee = await pair(world.engine, "queue-fails")
    world.scheduler.fails = True

    assert (await send(world.client, mentee, mentor)).status_code == 201


# --------------------------------------------------------------------------
# M2: the unread email
# --------------------------------------------------------------------------


async def test_one_unread_message_sends_the_few_email(world: World) -> None:
    mentor, mentee = await pair(world.engine, "few")
    message = await said(world, mentee, mentor, LONG)

    response = await fire(world, mentor)

    assert response.json() == {"queued": True}
    assert await rows(world.engine) == [("message_unread_few", "sent")]
    assert await emailed(world.engine, message["id"]) is not None
    [(notification, _, context)] = world.notifier.sent
    assert notification is Notification.MESSAGE_UNREAD_FEW
    assert context.extras["email_subject"] == "Mo sent you a message"
    assert context.extras["reply_to"] == str(mentee.id)
    assert context.extras["message1_text"].endswith("…")
    assert len(context.extras["message1_text"]) <= 140


async def test_a_message_read_before_the_15_minutes_sends_nothing(world: World) -> None:
    mentor, mentee = await pair(world.engine, "read-first")
    message = await said(world, mentee, mentor)
    await read_up_to(world, mentor, mentee, message["id"])

    assert (await fire(world, mentor)).json() == {"queued": False}
    assert await rows(world.engine) == []
    assert await emailed(world.engine, message["id"]) is None
    assert world.notifier.sent == []


async def test_a_second_batch_inside_the_hour_waits_for_it_to_end(world: World) -> None:
    mentor, mentee = await pair(world.engine, "hour")
    await said(world, mentee, mentor, "First")
    assert (await fire(world, mentor)).json() == {"queued": True}
    first_emailed = await scalar(
        world.engine,
        "SELECT max(emailed_at) FROM messages WHERE sender_id = :s",
        s=mentee.id,
    )
    later = await said(world, mentee, mentor, "Second")
    world.scheduler.published.clear()

    assert (await fire(world, mentor)).json() == {"queued": False}
    assert await emailed(world.engine, later["id"]) is None
    assert await rows(world.engine) == [("message_unread_few", "sent")]
    # Asked again for the moment the hour ends.
    assert world.scheduler.published == [
        {
            "url": f"{BASE}{PATH}",
            "body": {"recipient_id": str(mentor.id)},
            "at": held_until(first_emailed, first_emailed),
        }
    ]

    # Once the hour has passed, the waiting message goes.
    async with world.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE messages SET emailed_at = now() - interval '61 minutes' "
                "WHERE emailed_at IS NOT NULL"
            )
        )
    assert (await fire(world, mentor)).json() == {"queued": True}
    assert await emailed(world.engine, later["id"]) is not None


async def test_a_held_job_that_cannot_requeue_asks_qstash_to_retry(world: World) -> None:
    mentor, mentee = await pair(world.engine, "hour-retry")
    await said(world, mentee, mentor, "First")
    await fire(world, mentor)
    await said(world, mentee, mentor, "Second")
    world.scheduler.fails = True

    response = await fire(world, mentor)

    assert response.status_code >= 500


async def test_three_unread_send_the_many_email_with_no_text(world: World) -> None:
    mentor, mentee = await pair(world.engine, "many")
    other = await person(world.engine, "many-other", first_name="Ben")
    await book(world.engine, mentor, other, starts_in=dt.timedelta(days=5))
    for words in ("secret one", "secret two"):
        await said(world, mentee, mentor, words)
    await said(world, other, mentor, "secret three")

    assert (await fire(world, mentor)).json() == {"queued": True}

    [(notification, _, context)] = world.notifier.sent
    assert notification is Notification.MESSAGE_UNREAD_MANY
    assert context.extras["unread_count"] == "3"
    assert context.extras["sender_first_names"] == "Mo and Ben"
    assert context.extras["sender1_count"] == "2 messages"
    assert not any("secret" in value for value in context.extras.values())


async def test_switched_off_sends_no_unread_email(world: World) -> None:
    mentor, mentee = await pair(world.engine, "off")
    patched = await world.client.patch(
        SETTINGS, json={"unread_emails": False}, headers=mentor.headers
    )
    assert patched.json() == {"unread_emails": False}
    message = await said(world, mentee, mentor)

    assert (await fire(world, mentor)).json() == {"queued": False}
    assert await emailed(world.engine, message["id"]) is None


async def test_a_closed_account_gets_no_unread_email(world: World) -> None:
    mentor, mentee = await pair(world.engine, "closed")
    await said(world, mentee, mentor)
    await close_account(world.engine, mentor)

    assert (await fire(world, mentor)).json() == {"queued": False}


async def test_a_duplicate_delivery_sends_one_email(world: World) -> None:
    mentor, mentee = await pair(world.engine, "twice")
    await said(world, mentee, mentor)

    results = await asyncio.gather(fire(world, mentor), fire(world, mentor))

    assert sorted(r.json()["queued"] for r in results) == [False, True]
    assert len(world.notifier.sent) == 1


async def _chat(engine: AsyncEngine, mentor: Person, mentee: Person) -> UUID:
    async with engine.begin() as conn:
        return UUID(
            str(
                (
                    await conn.execute(
                        text(
                            "INSERT INTO conversations (status, mentor_user_id, mentee_user_id) "
                            "VALUES ('accepted', :m, :e) RETURNING id"
                        ),
                        {"m": mentor.id, "e": mentee.id},
                    )
                ).scalar_one()
            )
        )


async def _message(engine: AsyncEngine, chat: UUID, sender: Person) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO messages (conversation_id, sender_id, message_type, body) "
                "VALUES (:c, :s, 'text', 'Hi')"
            ),
            {"c": chat, "s": sender.id},
        )


@pytest.mark.parametrize("another_arrives", [False, True])
async def test_two_jobs_at_once_queue_one_email(
    db_engine: AsyncEngine, another_arrives: bool
) -> None:
    """The race held open: the first job's transaction is still open when the
    second starts. The second waits on the recipient's lock and, once the first
    commits, finds the message emailed; and a message that arrived meanwhile
    waits for the hour rather than going in a second email."""
    mentor, mentee = await pair(db_engine, f"race-{another_arrives}")
    chat = await _chat(db_engine, mentor, mentee)
    await _message(db_engine, chat, mentee)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    now = dt.datetime.now(dt.UTC)

    async with factory() as first, factory() as second:
        won = await queue_unread_email(first, mentor.id, now)
        if another_arrives:
            await _message(db_engine, chat, mentee)
        racing = asyncio.create_task(queue_unread_email(second, mentor.id, now))
        await asyncio.sleep(1)
        assert not racing.done(), "the second job did not wait for the first"
        await first.commit()
        lost = await racing
        await second.commit()

    assert won.queued is Notification.MESSAGE_UNREAD_FEW
    assert lost.queued is None
    assert lost.reason == ("held" if another_arrives else "nothing_unread")
    assert await scalar(db_engine, "SELECT count(*) FROM outbox_events") == 1


async def test_a_message_read_after_the_email_was_queued_is_not_sent(
    world: World, db_engine: AsyncEngine
) -> None:
    """Checked again when the email is sent: the provider was down when it was
    queued, and the recipient read it before the retry."""
    mentor, mentee = await pair(db_engine, "read-later")
    message = await said(world, mentee, mentor)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        await queue_unread_email(session, mentor.id, dt.datetime.now(dt.UTC))
        await session.commit()
    await read_up_to(world, mentor, mentee, message["id"])

    async with factory() as session:
        counts = await drain(
            session,
            notifier=world.notifier,
            now=dt.datetime.now(dt.UTC),
            settings=world.client._transport.app.state.settings,  # type: ignore[attr-defined]
        )
        await session.commit()

    assert counts["skipped"] == 1
    assert world.notifier.sent == []


async def test_a_deleted_message_is_never_emailed(world: World, db_engine: AsyncEngine) -> None:
    mentor, mentee = await pair(db_engine, "deleted")
    message = await said(world, mentee, mentor)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        await queue_unread_email(session, mentor.id, dt.datetime.now(dt.UTC))
        await session.commit()
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE messages SET deleted_at = now() WHERE id = :m"), {"m": message["id"]}
        )

    async with factory() as session:
        counts = await drain(
            session,
            notifier=world.notifier,
            now=dt.datetime.now(dt.UTC),
            settings=world.client._transport.app.state.settings,  # type: ignore[attr-defined]
        )
        await session.commit()

    assert counts["skipped"] == 1
    assert world.notifier.sent == []
    # And a deleted message is never gathered either.
    assert (await fire(world, mentor)).json() == {"queued": False}


# --------------------------------------------------------------------------
# M3: the session-window email
# --------------------------------------------------------------------------


async def test_a_message_in_the_session_window_is_emailed_at_once_in_full(world: World) -> None:
    mentor, mentee = await pair(
        world.engine, "window", starts_in=dt.timedelta(minutes=10), type_name="CV review"
    )
    message = await said(world, mentee, mentor, LONG)

    assert world.scheduler.published == []
    assert await emailed(world.engine, message["id"]) is not None
    [(notification, _, context)] = world.notifier.sent
    assert notification is Notification.MESSAGE_IN_SESSION_WINDOW
    assert context.extras["message1_text"] == LONG.strip()
    assert context.session_type_name == "CV review"
    assert context.duration_minutes == 45
    # It is never emailed again as unread.
    assert (await fire(world, mentor)).json() == {"queued": False}
    assert len(world.notifier.sent) == 1


async def test_the_window_email_goes_with_unread_emails_off(world: World) -> None:
    mentor, mentee = await pair(world.engine, "window-off", starts_in=-dt.timedelta(minutes=14))
    await world.client.patch(SETTINGS, json={"unread_emails": False}, headers=mentor.headers)

    await said(world, mentee, mentor)

    assert [n for n, _, _ in world.notifier.sent] == [Notification.MESSAGE_IN_SESSION_WINDOW]


async def test_outside_the_window_waits_for_the_unread_email(world: World) -> None:
    mentor, mentee = await pair(world.engine, "window-out", starts_in=dt.timedelta(minutes=16))
    message = await said(world, mentee, mentor)

    assert world.notifier.sent == []
    assert await emailed(world.engine, message["id"]) is None
    assert len(world.scheduler.published) == 1
    # The thread agrees: the same rule draws the session-window line.
    assert (await thread(world.client, mentee, mentor)).json()["session_window"] is False


# --------------------------------------------------------------------------
# The job's door
# --------------------------------------------------------------------------


async def test_an_unsigned_or_forged_delivery_is_refused(world: World) -> None:
    mentor, mentee = await pair(world.engine, "unsigned")
    await said(world, mentee, mentor)
    body = json.dumps({"recipient_id": str(mentor.id)}).encode()

    bare = await world.client.post(PATH, content=body)
    forged = await world.client.post(
        PATH, content=body, headers=signed(body, key="a-different-signing-key-nobody-configured")
    )
    swapped = await world.client.post(
        PATH, content=json.dumps({"recipient_id": str(uuid4())}).encode(), headers=signed(body)
    )

    assert [r.status_code for r in (bare, forged, swapped)] == [401, 401, 401]
    assert await rows(world.engine) == []


async def test_a_signed_body_that_is_not_a_job_is_a_422(world: World) -> None:
    body = json.dumps({"user_id": str(uuid4())}).encode()
    assert (await world.client.post(PATH, content=body, headers=signed(body))).status_code == 422


# --------------------------------------------------------------------------
# The switch
# --------------------------------------------------------------------------


async def test_the_switch_is_on_until_turned_off(world: World) -> None:
    me = await person(world.engine, "switch")

    assert (await world.client.get(SETTINGS, headers=me.headers)).json() == {"unread_emails": True}
    for value in (False, False, True):
        patched = await world.client.patch(
            SETTINGS, json={"unread_emails": value}, headers=me.headers
        )
        assert patched.status_code == 200
        assert patched.json() == {"unread_emails": value}
        assert (await world.client.get(SETTINGS, headers=me.headers)).json() == {
            "unread_emails": value
        }
    assert (
        await scalar(
            world.engine,
            "SELECT count(*) FROM notification_channel_preferences WHERE user_id = :u",
            u=me.id,
        )
        == 1
    )


@pytest.mark.parametrize(
    "body",
    [{}, {"unread_emails": "false"}, {"unread_emails": None}, {"unread_emails": True, "x": 1}],
)
async def test_the_switch_takes_one_boolean(world: World, body: dict[str, Any]) -> None:
    me = await person(world.engine, "switch-bad")
    response = await world.client.patch(SETTINGS, json=body, headers=me.headers)
    assert response.status_code == 422


async def test_the_switch_needs_a_signed_in_person(world: World) -> None:
    assert (await world.client.get(SETTINGS)).status_code == 401
    assert (await world.client.patch(SETTINGS, json={"unread_emails": False})).status_code == 401


async def test_one_persons_switch_is_theirs_alone(world: World) -> None:
    mentor, mentee = await pair(world.engine, "switch-own")
    await world.client.patch(SETTINGS, json={"unread_emails": False}, headers=mentee.headers)

    assert (await world.client.get(SETTINGS, headers=mentor.headers)).json() == {
        "unread_emails": True
    }


async def test_switching_off_stops_an_unread_email_already_queued(
    world: World, db_engine: AsyncEngine
) -> None:
    mentor, mentee = await pair(db_engine, "off-later")
    await said(world, mentee, mentor)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        await queue_unread_email(session, mentor.id, dt.datetime.now(dt.UTC))
        await session.commit()
    await world.client.patch(SETTINGS, json={"unread_emails": False}, headers=mentor.headers)

    async with factory() as session:
        counts = await drain(
            session,
            notifier=world.notifier,
            now=dt.datetime.now(dt.UTC),
            settings=world.client._transport.app.state.settings,  # type: ignore[attr-defined]
        )
        await session.commit()

    assert counts["skipped"] == 1
    assert world.notifier.sent == []


async def test_an_email_about_your_own_messages_does_not_hold_yours(world: World) -> None:
    """The hour is per recipient: the mentee being emailed about the mentor's
    message does not hold the mentor's email about the mentee's."""
    mentor, mentee = await pair(world.engine, "hour-own")
    await said(world, mentor, mentee, "From the mentor")
    assert (await fire(world, mentee)).json() == {"queued": True}
    await said(world, mentee, mentor, "From the mentee")

    assert (await fire(world, mentor)).json() == {"queued": True}


async def test_a_message_older_than_a_day_is_never_gathered(world: World) -> None:
    """An imported history, or messages that waited while the switch was off,
    never flood a first email."""
    mentor, mentee = await pair(world.engine, "lookback")
    old = await said(world, mentee, mentor, "Long ago")
    async with world.engine.begin() as conn:
        await conn.execute(
            text("UPDATE messages SET sent_at = now() - interval '25 hours' WHERE id = :m"),
            {"m": old["id"]},
        )

    assert (await fire(world, mentor)).json() == {"queued": False}
    fresh = await said(world, mentee, mentor, "Today")
    assert (await fire(world, mentor)).json() == {"queued": True}
    assert await emailed(world.engine, old["id"]) is None
    assert await emailed(world.engine, fresh["id"]) is not None


async def test_an_email_never_carries_a_message_from_someone_elses_chat(
    world: World, db_engine: AsyncEngine
) -> None:
    """The ids on the row are read only inside the recipient's own chats."""
    mentor, mentee = await pair(db_engine, "scope")
    stranger_mentor, stranger = await pair(db_engine, "scope-other")
    theirs = await said(world, stranger, stranger_mentor, "Not for you")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        await enqueue(
            session,
            Notification.MESSAGE_UNREAD_FEW,
            entity_type="user",
            entity_id=mentor.id,
            recipient_ids=(mentor.id,),
            variables={"message_ids": [theirs["id"]]},
        )
        await session.commit()
    del mentee

    async with factory() as session:
        counts = await drain(
            session,
            notifier=world.notifier,
            now=dt.datetime.now(dt.UTC),
            settings=world.client._transport.app.state.settings,  # type: ignore[attr-defined]
        )
        await session.commit()

    assert counts["skipped"] == 1
    assert world.notifier.sent == []


async def test_a_message_read_since_leaves_the_email_and_the_rest_still_go(
    world: World, db_engine: AsyncEngine
) -> None:
    mentor, mentee = await pair(db_engine, "read-one")
    first = await said(world, mentee, mentor, "First")
    await said(world, mentee, mentor, "Second")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        await queue_unread_email(session, mentor.id, dt.datetime.now(dt.UTC))
        await session.commit()
    await read_up_to(world, mentor, mentee, first["id"])

    async with factory() as session:
        await drain(
            session,
            notifier=world.notifier,
            now=dt.datetime.now(dt.UTC),
            settings=world.client._transport.app.state.settings,  # type: ignore[attr-defined]
        )
        await session.commit()

    [(notification, _, context)] = world.notifier.sent
    assert notification is Notification.MESSAGE_UNREAD_FEW
    assert context.extras["message1_text"] == "Second"
    assert "message2_text" not in context.extras
    assert "First" not in context.extras.values()


async def test_a_sender_who_closed_their_account_is_still_emailed_about_named_nobody(
    world: World,
) -> None:
    mentor, mentee = await pair(world.engine, "closed-sender")
    await said(world, mentee, mentor, "Before I go")
    await close_account(world.engine, mentee)

    assert (await fire(world, mentor)).json() == {"queued": True}
    [(_, _, context)] = world.notifier.sent
    assert context.extras["message1_name"] == "Someone"
    assert context.extras["message1_text"] == "Before I go"
