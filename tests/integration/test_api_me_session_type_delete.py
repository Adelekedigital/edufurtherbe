"""Deleting one of your own offerings, and the one thing that refuses it.

**Soft, never hard.** `sessions.session_type_id` is `RESTRICT`, so an offering
that was ever booked could not be hard-deleted anyway — and the row is what a
past session still points at, which is what keeps that mentee's history
readable. Deletion sets `deleted_at`; the reads stop returning it.

**`LIVE_STATUSES` is the refusal, reused rather than retyped.** It is the
predicate behind the double-booking exclusion constraint and three partial
indexes. A cancelled or completed session does not hold an offering open; a
session awaiting a decision or already agreed does, because it is somebody's
plan.

**One refusal, so no reason code.** The `409` used to need distinguishing from
the primary-offering refusal, and that one left with the pointer. A second
reason brings a machine-readable code back — the envelope already has a `type`
slot for it, and adding one is additive.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import add_session_type, make_public_mentor

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

URL = "/api/v1/me/session-types"


async def as_mentor(engine: AsyncEngine, tag: str) -> tuple[UUID, UUID]:
    mentor = await make_public_mentor(engine, tag)
    auth_id = uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET auth_id = :a WHERE id = :u"), {"a": auth_id, "u": mentor}
        )
    return mentor, auth_id


async def book(
    engine: AsyncEngine, mentor: UUID, session_type: UUID, *, status: str, days: int = 1
) -> None:
    """One session on a **named** offering.

    `factories.add_completed_sessions` picks the mentor's first session type with
    `LIMIT 1`, which cannot express "this offering and not that one" — and that
    distinction is what half this file tests.
    """
    async with engine.begin() as conn:
        mentee = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, first_name, primary_role, timezone) "
                    "VALUES (:e, 'Mentee', 'mentee', 'Africa/Lagos') RETURNING id"
                ),
                {"e": f"mentee-{uuid4()}@example.test"},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO sessions "
                "(mentor_id, mentee_id, session_type_id, starts_at, duration_minutes, status) "
                "VALUES (:m, :e, :t, :starts, 45, :status)"
            ),
            {
                "m": mentor,
                "e": mentee,
                "t": session_type,
                "starts": dt.datetime.now(dt.UTC) + dt.timedelta(days=days),
                "status": status,
            },
        )


async def deleted_at(engine: AsyncEngine, session_type: UUID) -> object:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT deleted_at FROM session_types WHERE id = :t"), {"t": session_type}
            )
        ).scalar_one()


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


async def test_an_unbooked_offering_is_deleted_and_the_row_survives(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """204, gone from the owner's list, and still in the table.

    The surviving row is the point rather than an implementation detail: a past
    session's `session_type_id` points at it, and a hard delete would take the
    thing that keeps that mentee's history readable.
    """
    mentor, auth_id = await as_mentor(db_engine, "delete-clean")
    session_type = await add_session_type(db_engine, mentor, name="SOP review")

    removed = await api_client.delete(f"{URL}/{session_type}", headers=bearer(api_token(auth_id)))
    assert removed.status_code == 204, removed.text

    listed = await api_client.get(URL, headers=bearer(api_token(auth_id)))
    assert listed.json()["data"] == []
    assert await deleted_at(db_engine, session_type) is not None


async def test_a_deleted_offering_leaves_the_public_list_too(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The owner's list and the shop window both stop returning it.

    Asserted separately because the two reads use different predicates —
    `session_type_of()` and `session_type_is_live()` — and soft deletion is the
    half they share. A deletion visible in only one of them is the failure.
    """
    mentor, auth_id = await as_mentor(db_engine, "delete-public")
    session_type = await add_session_type(db_engine, mentor, name="SOP review")

    await api_client.delete(f"{URL}/{session_type}", headers=bearer(api_token(auth_id)))

    public = await api_client.get(f"/api/v1/users/{mentor}/session-types")
    assert public.json()["data"] == []


async def test_the_freed_name_is_immediately_reusable(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The unique index is partial on `deleted_at IS NULL`, so deleting releases
    the name — and a mentor who deleted "SOP review" and could not recreate it
    would have no way to reach the row holding the name."""
    mentor, auth_id = await as_mentor(db_engine, "delete-reuse")
    session_type = await add_session_type(db_engine, mentor, name="SOP review")

    await api_client.delete(f"{URL}/{session_type}", headers=bearer(api_token(auth_id)))
    created = await api_client.post(
        URL,
        json={"name": "SOP review", "duration_minutes": 45},
        headers=bearer(api_token(auth_id)),
    )

    assert created.status_code == 201, created.text


async def test_a_switched_off_offering_can_still_be_deleted(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The scope is `session_type_of()`, so `is_active` is not consulted.

    Scoping on the live predicate would make a paused offering undeletable —
    and pausing before deleting is the obvious order for a mentor to work in.
    """
    mentor, auth_id = await as_mentor(db_engine, "delete-paused")
    session_type = await add_session_type(db_engine, mentor, name="Paused", active=False)

    removed = await api_client.delete(f"{URL}/{session_type}", headers=bearer(api_token(auth_id)))

    assert removed.status_code == 204, removed.text


# --------------------------------------------------------------------------
# The refusal — one test per status, not one per status code
# --------------------------------------------------------------------------


@pytest.mark.parametrize("live_status", ["pending_mentor_approval", "confirmed"])
async def test_a_live_session_refuses_the_delete(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, live_status: str
) -> None:
    """**One test per status, because `LIVE_STATUSES` has two members.**

    A single test would pass against a predicate that had lost one of them —
    and losing `pending_mentor_approval` is the likelier half, since a request
    nobody has answered yet is easy to read as "not really booked". It is
    somebody's plan either way.
    """
    mentor, auth_id = await as_mentor(db_engine, f"refuse-{live_status}")
    session_type = await add_session_type(db_engine, mentor, name="Booked")
    await book(db_engine, mentor, session_type, status=live_status)

    refused = await api_client.delete(f"{URL}/{session_type}", headers=bearer(api_token(auth_id)))

    assert refused.status_code == 409, refused.text


async def test_a_refused_delete_leaves_the_offering_alone(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The silent failure is a 409 that deleted anyway.**

    Asserting the status alone would pass against a store that raised after
    writing, or that wrote and then raised on the way out — and the offering
    would be gone from every read while the client believed it had been kept.
    """
    mentor, auth_id = await as_mentor(db_engine, "refuse-intact")
    session_type = await add_session_type(db_engine, mentor, name="Booked")
    await book(db_engine, mentor, session_type, status="confirmed")

    await api_client.delete(f"{URL}/{session_type}", headers=bearer(api_token(auth_id)))

    assert await deleted_at(db_engine, session_type) is None
    listed = await api_client.get(URL, headers=bearer(api_token(auth_id)))
    assert len(listed.json()["data"]) == 1


@pytest.mark.parametrize("terminal", ["completed", "cancelled", "declined", "no_show"])
async def test_a_terminal_session_does_not_hold_an_offering_open(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, terminal: str
) -> None:
    """The refusal is `LIVE_STATUSES`, not "has any session at all".

    A mentor whose offering ran for a year cannot be told it is permanently
    undeletable because it was once used — which is what a predicate reading
    `EXISTS (SELECT 1 FROM sessions ...)` would do.
    """
    mentor, auth_id = await as_mentor(db_engine, f"terminal-{terminal}")
    session_type = await add_session_type(db_engine, mentor, name="Finished")
    await book(db_engine, mentor, session_type, status=terminal, days=-30)

    removed = await api_client.delete(f"{URL}/{session_type}", headers=bearer(api_token(auth_id)))

    assert removed.status_code == 204, removed.text


async def test_a_booking_on_another_offering_does_not_refuse_this_one(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The check is scoped to the offering being deleted.

    A predicate keyed on the mentor rather than the session type would make one
    booked offering freeze every other one they own.
    """
    mentor, auth_id = await as_mentor(db_engine, "other-booked")
    booked = await add_session_type(db_engine, mentor, name="Booked")
    spare = await add_session_type(db_engine, mentor, name="Spare")
    await book(db_engine, mentor, booked, status="confirmed")

    removed = await api_client.delete(f"{URL}/{spare}", headers=bearer(api_token(auth_id)))

    assert removed.status_code == 204, removed.text


# --------------------------------------------------------------------------
# Not yours, and already gone
# --------------------------------------------------------------------------


async def test_another_mentors_offering_is_not_found(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`404`, not `403`: the latter confirms the id exists. Scoped in the query,
    so the row is never fetched rather than fetched and refused."""
    _, auth_id = await as_mentor(db_engine, "delete-mine")
    other = await make_public_mentor(db_engine, "delete-theirs")
    theirs = await add_session_type(db_engine, other, name="Theirs")

    refused = await api_client.delete(f"{URL}/{theirs}", headers=bearer(api_token(auth_id)))

    assert refused.status_code == 404, refused.text
    assert await deleted_at(db_engine, theirs) is None, "the 404 deleted it anyway"


async def test_deleting_twice_is_not_found(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Soft deletion is the other half of `session_type_of()`, so the second
    call cannot find the row it already removed."""
    mentor, auth_id = await as_mentor(db_engine, "delete-twice")
    session_type = await add_session_type(db_engine, mentor, name="Once")

    await api_client.delete(f"{URL}/{session_type}", headers=bearer(api_token(auth_id)))
    again = await api_client.delete(f"{URL}/{session_type}", headers=bearer(api_token(auth_id)))

    assert again.status_code == 404, again.text
