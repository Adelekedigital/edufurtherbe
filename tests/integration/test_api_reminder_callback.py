"""A scheduled reminder arriving, and the request it may no longer be about.

**The re-read is what this file exists to prove.** Scheduling ahead normally
obliges every transition to unschedule, and the bug is the reminder that fires
for a request answered through the one path somebody forgot. Checking at
delivery makes that unreachable — so there is a case here for *every* way a
request can stop waiting, not just one.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
from typing import Any
from uuid import uuid4

import httpx
import jwt
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import add_availability, add_session_type, make_public_mentor

from app.core.config import Settings
from app.domain.notifications import Notification
from app.infra.auth.supabase import SupabaseTokenVerifier
from app.infra.db.engine import create_session_factory
from app.main import create_app
from conftest import SECRET, api_token, bearer, fund_by_auth

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

BASE = "https://api.example.test"
PATH = "/api/v1/callbacks/reminders"
SIGNING_KEY = "not-a-real-signing-key-for-local-tests"


@pytest.fixture
def callback_client(db_engine: AsyncEngine) -> Any:
    """An app configured to believe QStash, which the default fixture is not.

    Built here rather than in `conftest` because every other suite wants the
    opposite: an unconfigured verifier that rejects everything, so no other test
    can accidentally reach a signed endpoint.
    """
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        qstash_current_signing_key=SIGNING_KEY,
        public_base_url=BASE,
    )
    app = create_app(settings)
    app.state.session_factory = create_session_factory(db_engine)
    app.state.token_verifier = SupabaseTokenVerifier(secret=SECRET)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def signed_headers(body: bytes, *, key: str = SIGNING_KEY) -> dict[str, str]:
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


async def a_pending_request(
    engine: AsyncEngine, client: httpx.AsyncClient, tag: str
) -> dict[str, Any]:
    """A booking on an offering that waits for the mentor."""
    mentor = await make_public_mentor(engine, tag)
    session_type = await add_session_type(engine, mentor, duration=60, notice=0)
    for day in range(7):
        await add_availability(engine, mentor, day_of_week=day, start="00:00", end="23:00")
    mentor_auth, mentee_auth = uuid4(), uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET auth_id = :a WHERE id = :u"), {"a": mentor_auth, "u": mentor}
        )
        await conn.execute(
            text(
                "INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
                "VALUES (:e, :a, 'Mo', 'mentee', 'Africa/Lagos')"
            ),
            {"e": f"mentee-{tag}@example.test", "a": mentee_auth},
        )
        # Booking spends a credit from PR 6 onward; this mentee has to be
        # able to pay for the sessions the test makes.
        await fund_by_auth(conn, mentee_auth)
        await conn.execute(
            text(
                "UPDATE mentor_profiles SET requires_booking_confirmation = true WHERE user_id = :u"
            ),
            {"u": mentor},
        )
    slots = await client.get(
        f"/api/v1/users/{mentor}/availability/slots",
        params={"session_type_id": str(session_type)},
    )
    created = await client.post(
        "/api/v1/sessions",
        json={
            "session_type_id": str(session_type),
            "starts_at": str(slots.json()["data"][-1]["start"]),
        },
        headers=bearer(api_token(mentee_auth)) | {"Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 201, created.text
    return {
        "id": created.json()["id"],
        "mentor": mentor,
        "mentor_headers": bearer(api_token(mentor_auth)),
        "mentee_headers": bearer(api_token(mentee_auth)),
    }


async def fire(client: httpx.AsyncClient, session_id: str, *, kind: str = "t12") -> Any:
    body = json.dumps({"session_id": session_id, "kind": kind}).encode()
    return await client.post(PATH, content=body, headers=signed_headers(body))


async def reminders(engine: AsyncEngine, session_id: str) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT payload FROM outbox_events WHERE entity_id = :i AND event_type = :t"
                    ),
                    {"i": session_id, "t": Notification.MENTOR_RESPONSE_REMINDER.value},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# The happy path, and the retry
# --------------------------------------------------------------------------


async def test_a_reminder_for_a_waiting_request_is_queued_for_the_mentor(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, callback_client: Any
) -> None:
    request = await a_pending_request(db_engine, api_client, "cb-queue")

    async with callback_client as client:
        fired = await fire(client, request["id"])

    assert fired.status_code == 200, fired.text
    assert fired.json() == {"queued": True}
    (queued,) = await reminders(db_engine, request["id"])
    assert queued["payload"]["recipient_id"] == str(request["mentor"])
    assert queued["payload"]["kind"] == "t12"


async def test_the_same_callback_twice_queues_one_reminder(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, callback_client: Any
) -> None:
    """**QStash retries by design**, so this is the ordinary case rather than an
    edge. A second identical email is exactly what a mentor notices, and the
    unique index — not the re-read — is what stops it: two callbacks arriving
    together would both see a pending request."""
    request = await a_pending_request(db_engine, api_client, "cb-twice")

    async with callback_client as client:
        first = await fire(client, request["id"])
        second = await fire(client, request["id"])

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(await reminders(db_engine, request["id"])) == 1


async def test_the_two_kinds_are_separate_reminders(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, callback_client: Any
) -> None:
    """The index keys on the kind, so `t24` and `t12` do not collide — a
    constraint on session alone would silently swallow the second nudge."""
    request = await a_pending_request(db_engine, api_client, "cb-kinds")

    async with callback_client as client:
        await fire(client, request["id"], kind="t24")
        await fire(client, request["id"], kind="t12")

    assert {r["payload"]["kind"] for r in await reminders(db_engine, request["id"])} == {
        "t24",
        "t12",
    }


# --------------------------------------------------------------------------
# The re-read — every way a request stops waiting
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "actor"),
    [("accept", "mentor_headers"), ("decline", "mentor_headers"), ("withdraw", "mentee_headers")],
)
async def test_an_answered_request_gets_no_reminder(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    callback_client: Any,
    action: str,
    actor: str,
) -> None:
    """**One case per transition, because that is the failure mode.** The
    alternative design makes each of these responsible for cancelling the
    scheduled work, and the bug is the one somebody forgets — so the test is
    parametrised over all of them rather than asserting the shape once."""
    request = await a_pending_request(db_engine, api_client, f"cb-{action}")
    await api_client.post(f"/api/v1/sessions/{request['id']}/{action}", headers=request[actor])

    async with callback_client as client:
        fired = await fire(client, request["id"])

    assert fired.status_code == 200, fired.text
    assert fired.json() == {"queued": False}, "a settled request was reminded about"
    assert await reminders(db_engine, request["id"]) == []


async def test_an_expired_request_gets_no_reminder(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, callback_client: Any
) -> None:
    """The fourth way, and the one no person causes — so the transition tests
    above could not have covered it."""
    request = await a_pending_request(db_engine, api_client, "cb-expired")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET status = 'expired' WHERE id = :i"), {"i": request["id"]}
        )

    async with callback_client as client:
        fired = await fire(client, request["id"])

    assert fired.json() == {"queued": False}


async def test_a_callback_for_a_session_that_does_not_exist_is_quiet(
    callback_client: Any,
) -> None:
    """A `404` would make QStash retry something that will never succeed. There
    is nothing to do and nothing to fix, so the honest answer is *nothing was
    queued*."""
    async with callback_client as client:
        fired = await fire(client, str(uuid4()))

    assert fired.status_code == 200, fired.text
    assert fired.json() == {"queued": False}


# --------------------------------------------------------------------------
# Believing the caller
# --------------------------------------------------------------------------


async def test_an_unsigned_callback_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, callback_client: Any
) -> None:
    request = await a_pending_request(db_engine, api_client, "cb-unsigned")

    async with callback_client as client:
        refused = await client.post(PATH, json={"session_id": request["id"], "kind": "t12"})

    assert refused.status_code == 401, refused.text
    assert await reminders(db_engine, request["id"]) == []


async def test_a_signature_from_the_wrong_key_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, callback_client: Any
) -> None:
    request = await a_pending_request(db_engine, api_client, "cb-wrongkey")
    body = json.dumps({"session_id": request["id"], "kind": "t12"}).encode()

    async with callback_client as client:
        refused = await client.post(
            PATH,
            content=body,
            headers=signed_headers(body, key="somebody-elses-signing-key-for-local-tests"),
        )

    assert refused.status_code == 401, refused.text
    assert await reminders(db_engine, request["id"]) == []


async def test_a_body_swapped_after_signing_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, callback_client: Any
) -> None:
    """**The attack the body hash exists for.** Somebody who observed one
    callback replays its signature against a session of their choosing — and
    this endpoint queues messages, so without the hash that is a
    send-anything primitive."""
    mine = await a_pending_request(db_engine, api_client, "cb-mine")
    theirs = await a_pending_request(db_engine, api_client, "cb-theirs")
    signed_for = json.dumps({"session_id": mine["id"], "kind": "t12"}).encode()
    sent_instead = json.dumps({"session_id": theirs["id"], "kind": "t12"}).encode()

    async with callback_client as client:
        refused = await client.post(PATH, content=sent_instead, headers=signed_headers(signed_for))

    assert refused.status_code == 401, refused.text
    assert await reminders(db_engine, theirs["id"]) == []


async def test_an_unconfigured_verifier_refuses_everything(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The default deployment, and it must reject rather than wave through.**
    A public endpoint that queues messages with no verifier configured is
    quietly open; one that rejects everything is visibly broken, which is the
    better of the two.

    `api_client` is that deployment — no signing key, no base URL.
    """
    request = await a_pending_request(db_engine, api_client, "cb-unconfigured")
    body = json.dumps({"session_id": request["id"], "kind": "t12"}).encode()

    refused = await api_client.post(PATH, content=body, headers=signed_headers(body))

    assert refused.status_code == 401, refused.text


async def test_a_body_that_is_not_a_reminder_is_refused(
    callback_client: Any,
) -> None:
    """Signed by QStash and still not something this endpoint can act on — a
    `kind` outside the schedule is a caller error rather than an intruder."""
    body = json.dumps({"session_id": str(uuid4()), "kind": "whenever"}).encode()

    async with callback_client as client:
        refused = await client.post(PATH, content=body, headers=signed_headers(body))

    assert refused.status_code == 422, refused.text
