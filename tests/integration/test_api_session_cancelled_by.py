"""Who called a session off, on the session read (FE bookings request #11).

`cancelled_by` names the mentor or the mentee when one of them cancelled as
themselves, and is null for everything else: an admin, the system, a migrated
cancellation that names nobody, and every status other than `cancelled`. It is
read off the newest `cancelled` event by the rule the mentor dashboard already
counts with (`session_stats.canceller`), so the card and the chart cannot
disagree about who cancelled.

The two parties cancel through the endpoint; the actors no endpoint can produce
yet (admin, system) are written as event rows, which is what a future writer
for them would leave behind.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_attendance import a_confirmed_session
from tests.integration.test_api_cancellation_refunds import cancel
from tests.integration.test_api_sessions import make_session, pair, sessions_url

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def read_both(
    client: httpx.AsyncClient, session_id: str, user_id: UUID, headers: dict[str, str]
) -> tuple[Any, Any]:
    """`cancelled_by` from the detail and from the user's list, which must agree."""
    detail = await client.get(f"/api/v1/sessions/{session_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    listed = await client.get(sessions_url(user_id), headers=headers)
    assert listed.status_code == 200, listed.text
    (row,) = [s for s in listed.json()["data"] if s["id"] == session_id]
    return detail.json()["cancelled_by"], row["cancelled_by"]


async def add_event(
    engine: AsyncEngine,
    session_id: UUID,
    *,
    actor_type: str,
    actor_id: UUID | None = None,
    to_status: str = "cancelled",
    at: dt.datetime | None = None,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO session_events (session_id, to_status, actor_id, actor_type, "
                "created_at) VALUES (:s, :t, :a, :y, COALESCE(:at, now()))"
            ),
            {"s": session_id, "t": to_status, "a": actor_id, "y": actor_type, "at": at},
        )


@pytest.mark.parametrize("by", ["mentor", "mentee"])
async def test_a_party_cancelling_is_named(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, by: str
) -> None:
    """Either party sees who it was, on the list and the detail alike."""
    booking = await a_confirmed_session(
        db_engine, api_client, f"cb-{by}", starts_in=dt.timedelta(hours=13)
    )
    cancelled = await cancel(api_client, booking, by)
    assert cancelled.status_code == 200, cancelled.text

    for viewer in ("mentor", "mentee"):
        assert await read_both(
            api_client, booking["id"], booking[f"{viewer}_id"], booking[viewer]
        ) == (by, by)


async def test_a_session_that_is_not_cancelled_has_nobody(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_confirmed_session(
        db_engine, api_client, "cb-live", starts_in=dt.timedelta(hours=13)
    )

    assert await read_both(api_client, booking["id"], booking["mentee_id"], booking["mentee"]) == (
        None,
        None,
    )


@pytest.mark.parametrize("actor_type", ["admin", "system", "api"])
async def test_nobody_but_a_party_acting_as_themselves_is_named(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, actor_type: str
) -> None:
    """An admin is null **even when the admin is the mentor**: acting through
    the console is a different fact from acting as themselves."""
    mentor, mentor_auth, mentee, _ = await pair(db_engine, f"cb-{actor_type}")
    session_id = await make_session(db_engine, mentor, mentee)
    await add_event(db_engine, session_id, actor_type=actor_type, actor_id=mentor)

    assert await read_both(api_client, str(session_id), mentor, bearer(api_token(mentor_auth))) == (
        None,
        None,
    )


async def test_a_migrated_cancellation_naming_nobody_is_null(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Legacy `Canceled By` that resolved to no user, and a cancelled row with
    no event at all."""
    mentor, mentor_auth, mentee, _ = await pair(db_engine, "cb-legacy")
    unnamed = await make_session(db_engine, mentor, mentee)
    await add_event(db_engine, unnamed, actor_type="user")
    eventless = await make_session(db_engine, mentor, mentee)
    headers = bearer(api_token(mentor_auth))

    assert await read_both(api_client, str(unnamed), mentor, headers) == (None, None)
    assert await read_both(api_client, str(eventless), mentor, headers) == (None, None)


async def test_the_newest_cancellation_is_the_one_read(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentor_auth, mentee, _ = await pair(db_engine, "cb-newest")
    session_id = await make_session(db_engine, mentor, mentee)
    now = dt.datetime.now(dt.UTC)
    await add_event(
        db_engine, session_id, actor_type="user", actor_id=mentee, at=now - dt.timedelta(hours=1)
    )
    await add_event(db_engine, session_id, actor_type="user", actor_id=mentor, at=now)
    # A later event to another status is not a cancellation.
    await add_event(
        db_engine,
        session_id,
        actor_type="user",
        actor_id=mentee,
        to_status="confirmed",
        at=now + dt.timedelta(minutes=1),
    )

    assert await read_both(api_client, str(session_id), mentor, bearer(api_token(mentor_auth))) == (
        "mentor",
        "mentor",
    )


async def test_a_row_contradicting_its_own_event_is_read_by_its_status(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A `cancelled` event on a session whose status says otherwise is not a
    cancellation (decision 101): the status column is what the field is about."""
    mentor, mentor_auth, mentee, _ = await pair(db_engine, "cb-contradict")
    session_id = await make_session(db_engine, mentor, mentee, status="completed")
    await add_event(db_engine, session_id, actor_type="user", actor_id=mentor)

    assert await read_both(api_client, str(session_id), mentor, bearer(api_token(mentor_auth))) == (
        None,
        None,
    )
