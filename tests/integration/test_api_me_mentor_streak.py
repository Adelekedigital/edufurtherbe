"""`current_streak` on `GET /me/mentor-stats` (decision 250): the caller's held
sessions in a row since their own last failure.

**Only the mentor's own failures break it**: a session they missed, or one they
cancelled acting as themselves. Everything else neither extends nor breaks it.
Each test that says "does not reset" also has held sessions on both sides of the
event, so a reset would show as a smaller number, not as the same zero.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import (
    add_session,
    cancelled,
    ended_by,
    make_bookable_mentor,
    token_for,
)

from conftest import bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]

URL = "/api/v1/me/mentor-stats"


async def streak(client: httpx.AsyncClient, engine: AsyncEngine, user: Any) -> int:
    response = await client.get(URL, headers=bearer(await token_for(engine, user)))
    assert response.status_code == 200, response.text
    return int(response.json()["current_streak"])


async def held(engine: AsyncEngine, mentor: Any, *days_ago: int) -> None:
    for ago in days_ago:
        await add_session(engine, mentor, days_ago=ago)


async def test_held_sessions_in_a_row_are_the_streak(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "streak-three")
    await held(db_engine, mentor, 3, 2, 1)

    assert await streak(api_client, db_engine, mentor) == 3


async def test_a_session_the_mentor_missed_resets_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "streak-missed")
    await held(db_engine, mentor, 6, 5)
    await add_session(db_engine, mentor, status="no_show", attendance="no_show", days_ago=4)
    await held(db_engine, mentor, 2, 1)

    assert await streak(api_client, db_engine, mentor) == 2


async def test_a_cancellation_by_the_mentor_resets_it_when_it_was_made(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Placed by `cancelled_at`, not `starts_at`: a session that was for ten
    days ago, cancelled three days ago, breaks the streak three days ago."""
    mentor = await make_bookable_mentor(db_engine, "streak-cancel-when")
    await held(db_engine, mentor, 5)
    await cancelled(db_engine, mentor, "mentor", days_ago=10, cancelled_days_ago=3)
    await held(db_engine, mentor, 2)

    assert await streak(api_client, db_engine, mentor) == 1


async def test_cancelling_a_future_session_today_resets_it_now(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "streak-cancel-future")
    await held(db_engine, mentor, 3, 2, 1)
    await cancelled(db_engine, mentor, "mentor", days_ago=-10, cancelled_days_ago=0)

    assert await streak(api_client, db_engine, mentor) == 0


async def test_somebody_elses_ending_neither_resets_nor_extends_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Cancelled by the mentee or the system, a request the mentor declined,
    one that expired, one the mentee withdrew: none is the mentor's failure."""
    mentor = await make_bookable_mentor(db_engine, "streak-not-mine")
    await held(db_engine, mentor, 6, 5)
    await cancelled(db_engine, mentor, "mentee", days_ago=4)
    await cancelled(db_engine, mentor, "system", days_ago=4)
    for status in ("declined", "expired", "withdrawn"):
        ended = await add_session(
            db_engine, mentor, status=status, attendance="pending", days_ago=4
        )
        await ended_by(db_engine, ended, mentor, status)
    await held(db_engine, mentor, 2)

    assert await streak(api_client, db_engine, mentor) == 3


async def test_an_admin_acting_under_the_mentors_id_does_not_reset_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Decision 247: the same id acting through the console is not the mentor."""
    mentor = await make_bookable_mentor(db_engine, "streak-admin")
    await held(db_engine, mentor, 5)
    await cancelled(db_engine, mentor, "admin", days_ago=3)
    await held(db_engine, mentor, 2)

    assert await streak(api_client, db_engine, mentor) == 2


async def test_the_mentees_no_show_neither_resets_nor_extends_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "streak-mentee-absent")
    await held(db_engine, mentor, 5)
    await add_session(
        db_engine,
        mentor,
        status="no_show",
        attendance="attended",
        mentee_attendance="no_show",
        days_ago=3,
    )
    await held(db_engine, mentor, 2)

    assert await streak(api_client, db_engine, mentor) == 2


async def test_upcoming_sessions_are_ignored(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "streak-upcoming")
    await held(db_engine, mentor, 2, 1)
    await add_session(db_engine, mentor, status="confirmed", attendance="pending", days_ago=-3)
    await add_session(
        db_engine, mentor, status="pending_mentor_approval", attendance="pending", days_ago=-4
    )

    assert await streak(api_client, db_engine, mentor) == 2


async def test_another_mentors_sessions_are_ignored(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The other mentor's later miss and cancellation would reset the caller's
    streak, and their held sessions would lengthen it, were either read."""
    mentor = await make_bookable_mentor(db_engine, "streak-mine")
    other = await make_bookable_mentor(db_engine, "streak-theirs")
    await held(db_engine, mentor, 5, 4)
    await add_session(db_engine, other, status="no_show", attendance="no_show", days_ago=3)
    await cancelled(db_engine, other, "mentor", days_ago=3)
    await held(db_engine, other, 2, 1)

    assert await streak(api_client, db_engine, mentor) == 2
    assert await streak(api_client, db_engine, other) == 2


async def test_the_callers_sessions_as_a_mentee_are_ignored(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Sessions the caller received, held or missed, are not ones they hosted."""
    mentor = await make_bookable_mentor(db_engine, "streak-as-mentee")
    host = await make_bookable_mentor(db_engine, "streak-host")
    await held(db_engine, mentor, 5)
    await add_session(db_engine, host, mentee=mentor, days_ago=4)
    await add_session(
        db_engine,
        host,
        mentee=mentor,
        status="no_show",
        attendance="attended",
        mentee_attendance="no_show",
        days_ago=3,
    )
    await held(db_engine, mentor, 2)

    assert await streak(api_client, db_engine, mentor) == 2


async def test_a_non_mentor_reads_zero(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    async with db_engine.begin() as conn:
        user = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, primary_role, timezone) "
                    "VALUES (:e, 'mentee', 'UTC') RETURNING id"
                ),
                {"e": f"plain-{uuid4()}@example.test"},
            )
        ).scalar_one()

    assert await streak(api_client, db_engine, user) == 0
