"""The new-message push end to end (decision 312).

Each new message queues its push job a few seconds later; the job is the signed
callback, delivered here by hand. What is pushed is read when the job runs, so
a message read or followed by another is not pushed. The fakes stand on
`app.state.scheduler` and `app.state.web_push`, the seams the code reads.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.message_helpers import Person, close_account, pair, scalar, send
from tests.integration.test_api_message_emails import (
    APP,
    BASE,
    SIGNING_KEY,
    FakeScheduler,
    read_up_to,
    signed,
)
from tests.unit.test_push_config import SUBJECT, vapid_pair

from app.domain.push import MESSAGE_PUSH_DELAY, PUSH_TTL
from app.infra.clients.web_push import PushOutcome, PushTarget

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

PATH = "/api/v1/callbacks/push-messages"
SUBS = "/api/v1/me/push-subscriptions"
FCM = "https://fcm.googleapis.com/fcm/send/"


@dataclass
class FakePush:
    """The push services: what each browser was sent, and what it answers."""

    sent: list[tuple[str, dict[str, Any], dt.timedelta, str]] = field(default_factory=list)
    answers: dict[str, PushOutcome] = field(default_factory=dict)

    def send(
        self, target: PushTarget, payload: bytes, *, ttl: dt.timedelta, urgency: str
    ) -> PushOutcome:
        self.sent.append((target.endpoint, json.loads(payload), ttl, urgency))
        return self.answers.get(target.endpoint, PushOutcome.DELIVERED)


@dataclass
class World:
    client: httpx.AsyncClient
    engine: AsyncEngine
    scheduler: FakeScheduler
    push: FakePush


def _configure(api_client: httpx.AsyncClient, *, push: bool) -> Any:
    app = api_client._transport.app  # type: ignore[attr-defined]
    public, private = vapid_pair()
    update: dict[str, Any] = {
        "public_base_url": BASE,
        "app_base_url": APP,
        "qstash_current_signing_key": SecretStr(SIGNING_KEY),
    }
    # Off clears all three, so a world switched off later never reaches the
    # real sender (and the network) through settings it kept.
    update |= (
        {
            "vapid_public_key": public,
            "vapid_private_key": SecretStr(private),
            "vapid_subject": SUBJECT,
        }
        if push
        else {"vapid_public_key": None, "vapid_private_key": None, "vapid_subject": None}
    )
    app.state.settings = app.state.settings.model_copy(update=update)
    return app


@pytest.fixture
def world(db_engine: AsyncEngine, api_client: httpx.AsyncClient) -> World:
    app = _configure(api_client, push=True)
    scheduler, push = FakeScheduler(), FakePush()
    app.state.scheduler = scheduler
    app.state.web_push = push
    return World(api_client, db_engine, scheduler, push)


async def subscribe(world: World, who: Person) -> str:
    endpoint = FCM + uuid4().hex
    body = {"endpoint": endpoint, "keys": {"p256dh": "B" + "p" * 86, "auth": "a" * 22}}
    response = await world.client.post(SUBS, json=body, headers=who.headers)
    assert response.status_code == 201, response.text
    return endpoint


async def said(world: World, sender: Person, to: Person, text: str = "Hi") -> dict[str, Any]:
    response = await send(world.client, sender, to, text)
    assert response.status_code == 201, response.text
    return dict(response.json())


async def fire(world: World, message_id: str | UUID) -> httpx.Response:
    body = json.dumps({"message_id": str(message_id)}).encode()
    return await world.client.post(PATH, content=body, headers=signed(body, path=PATH))


def pushes(world: World) -> list[dict[str, Any]]:
    return [job for job in world.scheduler.published if job["url"] == f"{BASE}{PATH}"]


async def test_a_new_message_queues_its_push_and_a_replay_does_not(world: World) -> None:
    mentor, mentee = await pair(world.engine, "push-queue")
    client_id = uuid4()

    first = await send(world.client, mentee, mentor, "Hi", client_id=client_id)
    again = await send(world.client, mentee, mentor, "Hi", client_id=client_id)

    assert (first.status_code, again.status_code) == (201, 200)
    sent_at = dt.datetime.fromisoformat(first.json()["sent_at"])
    assert pushes(world) == [
        {
            "url": f"{BASE}{PATH}",
            "body": {"message_id": first.json()["id"]},
            "at": sent_at + MESSAGE_PUSH_DELAY,
        }
    ]


async def test_unconfigured_push_queues_nothing(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    app = _configure(api_client, push=False)
    scheduler = FakeScheduler()
    app.state.scheduler = scheduler
    mentor, mentee = await pair(db_engine, "push-off")

    assert (await send(api_client, mentee, mentor, "Hi")).status_code == 201

    assert [job for job in scheduler.published if job["url"] == f"{BASE}{PATH}"] == []


async def test_every_browser_of_the_recipient_gets_who_sent_it_never_what(
    world: World,
) -> None:
    mentor, mentee = await pair(world.engine, "push-send")
    laptop, phone = await subscribe(world, mentor), await subscribe(world, mentor)
    await subscribe(world, mentee)  # the sender's own browser is never pushed
    await said(world, mentee, mentor, "earlier")
    await read_up_to(world, mentor, mentee, (await said(world, mentee, mentor, "seen"))["id"])
    message = await said(world, mentee, mentor, "the secret plan")

    response = await fire(world, message["id"])

    assert response.status_code == 200, response.text
    assert response.json() == {"delivered": 2}
    expected = {
        "type": "message",
        "title": "New message",
        "body": "New message from Mo",
        "url": f"/messages?with={mentee.id}"
        "&utm_source=push&utm_medium=notification&utm_campaign=message",
        "tag": f"message:{mentee.id}",
        "badge": 1,
    }
    assert sorted(world.push.sent, key=lambda sent: sent[0]) == sorted(
        [(laptop, expected, PUSH_TTL, "normal"), (phone, expected, PUSH_TTL, "normal")],
        key=lambda sent: sent[0],
    )


async def test_a_message_read_before_the_job_is_not_pushed(world: World) -> None:
    mentor, mentee = await pair(world.engine, "push-read")
    await subscribe(world, mentor)
    message = await said(world, mentee, mentor)
    await read_up_to(world, mentor, mentee, message["id"])

    response = await fire(world, message["id"])

    assert response.json() == {"delivered": 0}
    assert world.push.sent == []


async def test_a_burst_pushes_once_from_its_last_message(world: World) -> None:
    mentor, mentee = await pair(world.engine, "push-burst")
    await subscribe(world, mentor)
    first = await said(world, mentee, mentor, "one")
    second = await said(world, mentee, mentor, "two")

    assert (await fire(world, first["id"])).json() == {"delivered": 0}
    assert (await fire(world, second["id"])).json() == {"delivered": 1}
    [(_, payload, _, _)] = world.push.sent
    assert payload["badge"] == 2


async def test_a_gone_browser_is_forgotten_and_a_failing_one_kept(world: World) -> None:
    mentor, mentee = await pair(world.engine, "push-gone")
    gone, failing, fine = [await subscribe(world, mentor) for _ in range(3)]
    world.push.answers = {gone: PushOutcome.GONE, failing: PushOutcome.FAILED}
    message = await said(world, mentee, mentor)

    response = await fire(world, message["id"])

    assert response.json() == {"delivered": 1}
    left = await scalar(
        world.engine,
        "SELECT array_agg(endpoint ORDER BY endpoint) FROM push_subscriptions WHERE user_id = :u",
        u=mentor.id,
    )
    assert left == sorted([failing, fine])


async def test_a_closed_account_and_a_deleted_message_are_not_pushed(world: World) -> None:
    mentor, mentee = await pair(world.engine, "push-closed")
    await subscribe(world, mentor)
    message = await said(world, mentee, mentor)
    await close_account(world.engine, mentor)

    assert (await fire(world, message["id"])).json() == {"delivered": 0}
    assert (await fire(world, uuid4())).json() == {"delivered": 0}
    assert world.push.sent == []


async def test_an_unsigned_or_malformed_delivery_is_refused(world: World) -> None:
    body = json.dumps({"message_id": str(uuid4())}).encode()

    unsigned = await world.client.post(
        PATH, content=body, headers={"Content-Type": "application/json"}
    )
    wrong_path = await world.client.post(PATH, content=body, headers=signed(body))
    malformed_body = json.dumps({"recipient_id": str(uuid4())}).encode()
    malformed = await world.client.post(
        PATH, content=malformed_body, headers=signed(malformed_body, path=PATH)
    )

    assert (unsigned.status_code, wrong_path.status_code) == (401, 401)
    assert malformed.status_code == 422
    assert world.push.sent == []


async def test_a_job_that_cannot_be_queued_never_fails_the_send(world: World) -> None:
    mentor, mentee = await pair(world.engine, "push-noqueue")
    world.scheduler.fails = True

    assert (await send(world.client, mentee, mentor, "Hi")).status_code == 201

    assert pushes(world) == []


async def test_with_no_public_origin_nothing_is_queued(world: World) -> None:
    app = world.client._transport.app  # type: ignore[attr-defined]
    app.state.settings = app.state.settings.model_copy(update={"public_base_url": None})
    mentor, mentee = await pair(world.engine, "push-noorigin")

    assert (await send(world.client, mentee, mentor, "Hi")).status_code == 201

    assert [job for job in world.scheduler.published if "message_id" in job["body"]] == []


async def test_push_switched_off_before_the_job_sends_nothing_and_keeps_browsers(
    world: World,
) -> None:
    mentor, mentee = await pair(world.engine, "push-offlater")
    await subscribe(world, mentor)
    message = await said(world, mentee, mentor)
    app = _configure(world.client, push=False)
    del app.state.web_push

    assert (await fire(world, message["id"])).json() == {"delivered": 0}
    assert (
        await scalar(
            world.engine, "SELECT count(*) FROM push_subscriptions WHERE user_id = :u", u=mentor.id
        )
        == 1
    )
