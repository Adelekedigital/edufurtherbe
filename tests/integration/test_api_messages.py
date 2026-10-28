"""Messages: who may read and write a chat, and sending (decision 301).

M0 decides access: two people are connected by a session that was booked
(confirmed, completed, no_show or cancelled). Reads answer `404` to anyone
else and sends `403 /problems/not-connected`. A paused mentor and a closed
account make the chat read only (`409`). Reads are in `test_api_message_reads`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.message_helpers import (
    ROOT,
    book,
    close_account,
    pair,
    person,
    scalar,
    send,
    thread,
)

from app.domain.chats import BODY_MAX_LENGTH
from app.domain.enums import SessionStatus

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

CONNECTING = ["confirmed", "completed", "no_show", "cancelled"]
NOT_CONNECTING = ["pending_mentor_approval", "declined", "expired", "withdrawn"]


async def test_the_lists_cover_every_status() -> None:
    assert set(CONNECTING) | set(NOT_CONNECTING) == {s.value for s in SessionStatus}


# --------------------------------------------------------------------------
# M0
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", CONNECTING)
async def test_a_booked_session_connects_the_two(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, status: str
) -> None:
    mentor, mentee = await pair(db_engine, f"m0-{status}", status=status)

    sent = await send(api_client, mentee, mentor, "Hi Ada")
    reply = await send(api_client, mentor, mentee, "Hi Mo")
    read = await thread(api_client, mentee, mentor)
    inbox = await api_client.get(ROOT, headers=mentor.headers)
    found = await api_client.get(f"{ROOT}/people", headers=mentee.headers)

    assert sent.status_code == 201, sent.text
    assert reply.status_code == 201, reply.text
    assert read.status_code == 200, read.text
    assert [m["body"] for m in read.json()["items"]] == ["Hi Ada", "Hi Mo"]
    assert [row["person"]["id"] for row in inbox.json()["data"]] == [str(mentee.id)]
    assert [p["id"] for p in found.json()["data"]] == [str(mentor.id)]


@pytest.mark.parametrize("status", NOT_CONNECTING)
async def test_a_request_never_agreed_is_no_connection(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, status: str
) -> None:
    mentor, mentee = await pair(db_engine, f"m0-not-{status}", status=status)

    sent = await send(api_client, mentee, mentor)
    read = await thread(api_client, mentee, mentor)
    marked = await api_client.post(
        f"{ROOT}/{mentor.id}/read", json={"up_to": str(uuid4())}, headers=mentee.headers
    )
    inbox = await api_client.get(ROOT, headers=mentee.headers)
    found = await api_client.get(f"{ROOT}/people", headers=mentee.headers)

    assert sent.status_code == 403
    assert sent.json()["type"] == "/problems/not-connected"
    assert read.status_code == 404
    assert read.json()["type"] == "about:blank"
    assert marked.status_code == 404
    assert inbox.json() == {"data": [], "next_cursor": None, "total_unread": 0}
    assert found.json()["data"] == []
    assert await scalar(db_engine, "SELECT count(*) FROM conversations") == 0


async def test_a_third_person_can_never_read_or_write_a_pair_s_chat(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A stranger connected to the mentor through their own booking reads only
    their own chat with the mentor, never the mentor's chat with the mentee,
    and a stranger connected to nobody reaches neither person."""
    mentor, mentee = await pair(db_engine, "third")
    other_mentee = await person(db_engine, "third-other", first_name="Zed")
    await book(db_engine, mentor, other_mentee, starts_in=dt.timedelta(days=5))
    nobody = await person(db_engine, "third-nobody")
    assert (await send(api_client, mentee, mentor, "Private to Ada")).status_code == 201

    theirs = await thread(api_client, other_mentee, mentor)
    unread = await api_client.get(f"{ROOT}/unread", headers=other_mentee.headers)
    for target in (mentor, mentee):
        assert (await thread(api_client, nobody, target)).status_code == 404
        assert (await send(api_client, nobody, target)).status_code == 403
    assert (await thread(api_client, other_mentee, mentee)).status_code == 404
    assert (await send(api_client, other_mentee, mentee)).status_code == 403

    assert theirs.status_code == 200
    assert theirs.json()["items"] == []
    assert unread.json() == {"total": 0}
    assert (await api_client.get(ROOT, headers=nobody.headers)).json()["data"] == []
    assert await scalar(db_engine, "SELECT count(*) FROM messages") == 1


async def test_nobody_and_yourself_look_like_strangers(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee = await pair(db_engine, "self")

    for target in (mentee.id, uuid4()):
        read = await thread(api_client, mentee, target)
        sent = await send(api_client, mentee, target)
        assert read.status_code == 404
        assert sent.status_code == 403
        assert sent.json()["type"] == "/problems/not-connected"
    assert (await thread(api_client, mentee, mentor)).status_code == 200


async def test_reads_and_writes_need_a_token(api_client: httpx.AsyncClient) -> None:
    other = uuid4()
    for response in (
        await api_client.get(ROOT),
        await api_client.get(f"{ROOT}/unread"),
        await api_client.get(f"{ROOT}/people"),
        await api_client.get(f"{ROOT}/{other}"),
        await api_client.post(f"{ROOT}/{other}", json={"client_id": str(uuid4()), "body": "x"}),
        await api_client.post(f"{ROOT}/{other}/read", json={"up_to": str(uuid4())}),
    ):
        assert response.status_code == 401


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------


async def test_the_first_send_opens_the_chat_and_later_ones_reuse_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee = await pair(db_engine, "send")
    client_id = uuid4()

    first = await send(
        api_client, mentee, mentor, "  Hi Ada,\nsee you soon.  ", client_id=client_id
    )
    second = await send(api_client, mentor, mentee, "Hello")

    assert first.status_code == 201, first.text
    assert first.headers["location"] == f"/api/v1/me/messages/{mentor.id}"
    body = first.json()
    assert set(body) == {
        "id",
        "client_id",
        "sender_id",
        "mine",
        "body",
        "file",
        "sent_at",
        "edited_at",
        "deleted",
    }
    assert body["body"] == "Hi Ada,\nsee you soon."
    assert body["client_id"] == str(client_id)
    assert body["sender_id"] == str(mentee.id)
    assert body["mine"] is True
    assert body["file"] is None
    assert body["deleted"] is False
    assert body["edited_at"] is None
    assert second.status_code == 201
    assert await scalar(db_engine, "SELECT count(*) FROM conversations") == 1
    assert await scalar(
        db_engine,
        "SELECT mentor_user_id = :m AND mentee_user_id = :e AND created_by = :e "
        "AND status = 'accepted' AND last_message_at = (SELECT max(sent_at) FROM messages) "
        "FROM conversations",
        m=mentor.id,
        e=mentee.id,
    )
    assert await scalar(db_engine, "SELECT count(*) FROM conversation_participants") == 2


async def test_a_repeated_client_id_returns_the_stored_message(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee = await pair(db_engine, "replay")
    client_id = uuid4()

    first = await send(api_client, mentee, mentor, "Once", client_id=client_id)
    again = await send(api_client, mentee, mentor, "Once", client_id=client_id)
    # The other person may use the same value: the key is per sender.
    theirs = await send(api_client, mentor, mentee, "Mine", client_id=client_id)

    assert first.status_code == 201
    assert again.status_code == 200
    assert again.json() == first.json()
    assert theirs.status_code == 201
    assert await scalar(db_engine, "SELECT count(*) FROM messages") == 2


async def test_a_client_id_from_another_chat_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee = await pair(db_engine, "replay-other")
    second_mentor = await person(db_engine, "replay-other-2", mentor="listed")
    await book(db_engine, second_mentor, mentee, starts_in=dt.timedelta(days=5))
    client_id = uuid4()
    assert (await send(api_client, mentee, mentor, client_id=client_id)).status_code == 201

    elsewhere = await send(api_client, mentee, second_mentor, client_id=client_id)

    assert elsewhere.status_code == 422
    assert elsewhere.json()["errors"][0]["pointer"] == "/client_id"
    assert await scalar(db_engine, "SELECT count(*) FROM messages") == 1


async def test_two_first_sends_at_once_open_one_chat(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee = await pair(db_engine, "race-open")

    results = await asyncio.gather(
        send(api_client, mentee, mentor, "From Mo"),
        send(api_client, mentor, mentee, "From Ada"),
    )

    assert [r.status_code for r in results] == [201, 201]
    assert await scalar(db_engine, "SELECT count(*) FROM conversations") == 1
    assert await scalar(db_engine, "SELECT count(DISTINCT conversation_id) FROM messages") == 1
    assert await scalar(db_engine, "SELECT count(*) FROM messages") == 2


async def test_a_retry_racing_its_original_stores_one_message(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee = await pair(db_engine, "race-replay")
    assert (await send(api_client, mentee, mentor, "Open")).status_code == 201
    client_id = uuid4()

    results = await asyncio.gather(
        *(send(api_client, mentee, mentor, "Twice", client_id=client_id) for _ in range(3))
    )

    assert sorted(r.status_code for r in results) == [200, 200, 201]
    assert len({r.json()["id"] for r in results}) == 1
    assert await scalar(db_engine, "SELECT count(*) FROM messages") == 2


@pytest.mark.parametrize(
    ("body", "ok"),
    [
        ("x" * BODY_MAX_LENGTH, True),
        ("  " + "x" * BODY_MAX_LENGTH + "\n ", True),
        ("x" * (BODY_MAX_LENGTH + 1), False),
        ("   \n  ", False),
        ("", False),
    ],
)
async def test_the_body_is_trimmed_and_bounded(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, body: str, ok: bool
) -> None:
    mentor, mentee = await pair(db_engine, f"bound-{len(body)}-{ok}")

    response = await send(api_client, mentee, mentor, body)

    if ok:
        assert response.status_code == 201, response.text
        assert len(response.json()["body"]) == BODY_MAX_LENGTH
    else:
        assert response.status_code == 422
        assert response.json()["errors"][0]["pointer"] == "/body"
        assert await scalar(db_engine, "SELECT count(*) FROM messages") == 0


async def test_a_body_this_endpoint_does_not_take_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee = await pair(db_engine, "extra")
    for payload in (
        {"client_id": str(uuid4())},
        {"body": "Hi"},
        {"client_id": str(uuid4()), "body": "Hi", "file_id": str(uuid4())},
    ):
        response = await api_client.post(
            f"{ROOT}/{mentor.id}", json=payload, headers=mentee.headers
        )
        assert response.status_code == 422, payload


# --------------------------------------------------------------------------
# Read only: paused and closed
# --------------------------------------------------------------------------


async def test_a_paused_mentor_cannot_be_sent_to_and_still_reads_and_writes(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await person(db_engine, "paused-mentor", slug="paused-ada", mentor="unlisted")
    mentee = await person(db_engine, "paused-mentee")
    await book(db_engine, mentor, mentee)
    client_id = uuid4()
    assert (await send(api_client, mentor, mentee, "Before")).status_code == 201

    refused = await send(api_client, mentee, mentor, client_id=client_id)
    read = await thread(api_client, mentee, mentor)
    theirs = await send(api_client, mentor, mentee, "Still here")

    assert refused.status_code == 409
    assert refused.json()["type"] == "/problems/recipient-paused"
    assert read.status_code == 200
    assert read.json()["lock"] == "paused"
    assert read.json()["person"]["paused"] is True
    assert read.json()["person"]["handle"] is None
    assert theirs.status_code == 201
    assert (await thread(api_client, mentor, mentee)).json()["lock"] is None


async def test_a_mentee_who_is_a_paused_mentor_elsewhere_is_not_locked(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Paused is about a mentor in this chat: somebody who is your mentee here
    and a paused mentor to others can still be written to."""
    mentor = await person(db_engine, "role-mentor", mentor="listed")
    mentee = await person(db_engine, "role-mentee", mentor="unlisted")
    await book(db_engine, mentor, mentee)

    sent = await send(api_client, mentor, mentee)
    read = await thread(api_client, mentor, mentee)

    assert sent.status_code == 201
    assert read.json()["person"]["role"] == "mentee"
    assert read.json()["person"]["paused"] is False
    assert read.json()["lock"] is None


async def test_a_closed_account_cannot_be_sent_to_and_closed_wins_over_paused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await person(db_engine, "closed-mentor", first_name="Ada", mentor="unlisted")
    mentee = await person(db_engine, "closed-mentee")
    await book(db_engine, mentor, mentee)
    assert (await send(api_client, mentor, mentee, "Bye")).status_code == 201
    await close_account(db_engine, mentor)

    refused = await send(api_client, mentee, mentor)
    read = await thread(api_client, mentee, mentor)

    assert refused.status_code == 409
    assert refused.json()["type"] == "/problems/recipient-closed"
    body = read.json()
    assert read.status_code == 200
    assert body["lock"] == "closed"
    assert body["person"]["deleted"] is True
    assert body["person"]["first_name"] is None
    assert body["person"]["paused"] is False
    assert [m["body"] for m in body["items"]] == ["Bye"]


async def test_a_retry_after_a_pause_still_gets_its_message(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee = await pair(db_engine, "replay-paused")
    client_id = uuid4()
    first = await send(api_client, mentee, mentor, client_id=client_id)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE mentor_profiles SET listing_status = 'unlisted' WHERE user_id = :u"),
            {"u": mentor.id},
        )

    again = await send(api_client, mentee, mentor, client_id=client_id)

    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
