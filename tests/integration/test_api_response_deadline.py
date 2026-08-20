"""The deadline an unanswered request dies on, and the sweep that kills it.

**The slot is the point, not the status.** `sessions_no_mentor_double_booking`
covers `pending_mentor_approval` and `slot_store._busy` counts it, so until
something writes a terminal status an abandoned request holds the mentor's hour
indefinitely. Every test here that matters ends by asking whether the hour came
back.

`respond_by` is `starts_at - 6h`, and six rather than twenty-four because the
mentor's time to answer is `(starts_at - booked_at) - W` against a 24-hour
booking floor. That arithmetic has its own unit test; these are about the
behaviour it produces.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.factories import add_availability, add_session_type, make_public_mentor

from app.infra.db.session_writer import expire_requests
from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def a_request(
    engine: AsyncEngine, client: httpx.AsyncClient, tag: str, *, confirmation: bool = True
) -> dict[str, Any]:
    """A booking on an offering that does — or does not — await an answer."""
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
        if confirmation:
            await conn.execute(
                text(
                    "UPDATE mentor_profiles SET requires_booking_confirmation = true "
                    "WHERE user_id = :u"
                ),
                {"u": mentor},
            )

    slots = await client.get(
        f"/api/v1/users/{mentor}/availability/slots",
        params={"session_type_id": str(session_type)},
    )
    starts_at = str(slots.json()["data"][-1]["start"])
    created = await client.post(
        "/api/v1/sessions",
        json={"session_type_id": str(session_type), "starts_at": starts_at},
        headers=bearer(api_token(mentee_auth)) | {"Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 201, created.text
    return {
        "id": created.json()["id"],
        "body": created.json(),
        "mentor": bearer(api_token(mentor_auth)),
        "mentee": bearer(api_token(mentee_auth)),
        "mentor_id": mentor,
        "session_type_id": session_type,
        "starts_at": starts_at,
    }


async def sweep(engine: AsyncEngine) -> int:
    """Run the producer the way the script does — its own session, then commit."""
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        expired = await expire_requests(session, now=dt.datetime.now(dt.UTC))
        await session.commit()
    return expired


async def status_of(engine: AsyncEngine, session_id: str) -> str:
    async with engine.connect() as conn:
        return str(
            (
                await conn.execute(
                    text("SELECT status FROM sessions WHERE id = :i"), {"i": session_id}
                )
            ).scalar_one()
        )


async def offered(client: httpx.AsyncClient, request: dict[str, Any]) -> list[str]:
    response = await client.get(
        f"/api/v1/users/{request['mentor_id']}/availability/slots",
        params={"session_type_id": str(request["session_type_id"])},
    )
    return [str(slot["start"]) for slot in response.json()["data"]]


async def make_overdue(engine: AsyncEngine, session_id: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET respond_by = now() - interval '1 minute' WHERE id = :i"),
            {"i": session_id},
        )


# --------------------------------------------------------------------------
# The deadline itself
# --------------------------------------------------------------------------


async def test_a_request_carries_a_deadline_six_hours_before_the_session(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Measured backwards from the session, not forwards from the request.

    The guarantee is to the *mentee* — you learn the answer before the session,
    early enough for it to be useful — and a window measured from `created_at`
    guarantees them nothing.
    """
    request = await a_request(db_engine, api_client, "rb-set")

    starts_at = dt.datetime.fromisoformat(request["starts_at"])
    respond_by = dt.datetime.fromisoformat(request["body"]["respond_by"])
    assert starts_at - respond_by == dt.timedelta(hours=6)


async def test_an_auto_confirming_offering_has_no_deadline(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Null is the domain rule, not an unset field.** Nothing is awaiting an
    answer, so there is no window to elapse — and writing one anyway would put
    confirmed sessions in the sweep's path."""
    request = await a_request(db_engine, api_client, "rb-auto", confirmation=False)

    assert request["body"]["status"] == "confirmed"
    assert request["body"]["respond_by"] is None


async def test_the_mentor_can_see_their_own_deadline(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A deadline the mentor cannot read is a deadline they cannot act on."""
    request = await a_request(db_engine, api_client, "rb-visible")

    seen = await api_client.get(f"/api/v1/sessions/{request['id']}", headers=request["mentor"])

    assert seen.json()["respond_by"] == request["body"]["respond_by"]


async def test_answering_leaves_the_deadline_on_the_row(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """It stays as a record of how long the mentor actually had. The index is
    partial on the pending status, so a settled row costs nothing — and clearing
    it would erase the only evidence of the window they were given."""
    request = await a_request(db_engine, api_client, "rb-kept")
    await api_client.post(f"/api/v1/sessions/{request['id']}/accept", headers=request["mentor"])

    seen = await api_client.get(f"/api/v1/sessions/{request['id']}", headers=request["mentor"])

    assert seen.json()["status"] == "confirmed"
    assert seen.json()["respond_by"] == request["body"]["respond_by"]


# --------------------------------------------------------------------------
# The sweep
# --------------------------------------------------------------------------


async def test_an_unanswered_request_expires_and_frees_its_slot(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The whole point, in one test.**

    Before the sweep the hour is gone from the public grid — a pending request
    is inside `LIVE_STATUSES` and `_busy` counts it. After the sweep it is back,
    because `expired` is in `FREES_THE_HOUR`.
    """
    request = await a_request(db_engine, api_client, "rb-expire")
    assert request["starts_at"] not in await offered(api_client, request)
    await make_overdue(db_engine, request["id"])

    assert await sweep(db_engine) == 1

    assert await status_of(db_engine, request["id"]) == "expired"
    assert request["starts_at"] in await offered(api_client, request)


async def test_a_request_still_inside_its_window_is_left_alone(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Expiring early takes the decision away from a mentor who still had time,
    and nothing can undo it — `expired` is terminal and no transition leads
    out."""
    request = await a_request(db_engine, api_client, "rb-early")

    assert await sweep(db_engine) == 0
    assert await status_of(db_engine, request["id"]) == "pending_mentor_approval"


async def test_a_confirmed_session_is_never_expired(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The guard that keeps the two sweeps disjoint.** An accepted session
    keeps its `respond_by` for the record, so without the status filter every
    answered request would be expired the moment its old deadline passed —
    silently cancelling sessions both parties had agreed to."""
    request = await a_request(db_engine, api_client, "rb-answered")
    await api_client.post(f"/api/v1/sessions/{request['id']}/accept", headers=request["mentor"])
    await make_overdue(db_engine, request["id"])

    assert await sweep(db_engine) == 0
    assert await status_of(db_engine, request["id"]) == "confirmed"


async def test_a_session_with_no_deadline_is_never_expired(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Auto-confirmed sessions and every migrated row carry a null `respond_by`.

    `NULL <= now()` is unknown rather than true, so the comparison alone would
    already exclude them — the explicit `IS NOT NULL` is there so a reader does
    not have to work that out, and so the index predicate and the query agree.
    """
    request = await a_request(db_engine, api_client, "rb-nodeadline", confirmation=False)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE sessions SET status = 'pending_mentor_approval', respond_by = NULL "
                "WHERE id = :i"
            ),
            {"i": request["id"]},
        )

    assert await sweep(db_engine) == 0
    assert await status_of(db_engine, request["id"]) == "pending_mentor_approval"


async def test_the_sweep_is_safe_to_run_twice(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """An external scheduler is the kind that fires twice, and a second event on
    an expired request would put a duplicate in a log meant to be the record of
    what happened."""
    request = await a_request(db_engine, api_client, "rb-idem")
    await make_overdue(db_engine, request["id"])
    assert await sweep(db_engine) == 1

    assert await sweep(db_engine) == 0

    events = await api_client.get(
        f"/api/v1/sessions/{request['id']}/events", headers=request["mentor"]
    )
    assert [event["to_status"] for event in events.json()["data"]] == [
        "pending_mentor_approval",
        "expired",
    ]


async def test_the_expiry_event_blames_nobody(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Nothing was declined and nobody withdrew. A null actor with
    `actor_type = system` is what says no person decided this, and
    `expired_no_response` is the one reason code no party is permitted to
    send — precisely because only a sweep can honestly produce it."""
    request = await a_request(db_engine, api_client, "rb-actor")
    await make_overdue(db_engine, request["id"])
    await sweep(db_engine)

    events = await api_client.get(
        f"/api/v1/sessions/{request['id']}/events", headers=request["mentee"]
    )

    expiry = events.json()["data"][-1]
    assert expiry["from_status"] == "pending_mentor_approval"
    assert expiry["to_status"] == "expired"
    assert expiry["actor_id"] is None
    assert expiry["actor_type"] == "system"
    assert expiry["reason_code"] == "expired_no_response"


async def test_an_expired_request_cannot_be_answered(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`expired` is terminal and appears in no transition's `allowed_from`, so a
    mentor arriving late is refused rather than reviving a slot somebody else
    may already have booked."""
    request = await a_request(db_engine, api_client, "rb-late")
    await make_overdue(db_engine, request["id"])
    await sweep(db_engine)

    refused = await api_client.post(
        f"/api/v1/sessions/{request['id']}/accept", headers=request["mentor"]
    )

    assert refused.status_code == 409, refused.text
    assert "expired" in refused.json()["detail"]


async def test_the_freed_slot_can_be_booked_by_somebody_else(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The end-to-end claim, and the one a count of expired rows does not make.

    A slot that reappears on the grid but is still refused by the exclusion
    constraint would be worse than one that never came back — the mentee would
    see it, click it, and be told it was taken.
    """
    request = await a_request(db_engine, api_client, "rb-rebook")
    await make_overdue(db_engine, request["id"])
    await sweep(db_engine)
    other = uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (email, auth_id, primary_role, timezone) "
                "VALUES (:e, :a, 'mentee', 'Africa/Lagos')"
            ),
            {"e": f"rebook-{other}@example.test", "a": other},
        )

    rebooked = await api_client.post(
        "/api/v1/sessions",
        json={
            "session_type_id": str(request["session_type_id"]),
            "starts_at": request["starts_at"],
        },
        headers=bearer(api_token(other)) | {"Idempotency-Key": str(uuid4())},
    )

    assert rebooked.status_code == 201, rebooked.text
