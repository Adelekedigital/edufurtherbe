"""Which offering the next free time belongs to (frontend request #18).

`next_available_at` is the earliest slot across a mentor's offerings. Saying
which offering it came from lets "Book {time}" open on that offering instead of
the client trying each one's slots in turn. The id is gated exactly like the
time: sent only while the state is `open`, null otherwise.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import add_availability, add_session_type, make_public_mentor
from tests.integration.test_mentor_next_available import card, refresh

pytestmark = [pytest.mark.db, pytest.mark.anyio]

#: Three days' notice pushes an offering's first slot well past one with none.
LONG_NOTICE = 3 * 24 * 60


async def two_offerings(engine: AsyncEngine, tag: str) -> tuple[UUID, UUID, UUID]:
    """A mentor with a slow offering (added first) and a quick one."""
    mentor = await make_public_mentor(engine, tag, slug=tag)
    # Named so the slow one is listed first (offerings sort by name): the
    # earliest slot must win, not the first offering listed.
    slow = await add_session_type(engine, mentor, name="A slow review", notice=LONG_NOTICE)
    quick = await add_session_type(engine, mentor, name="Z quick chat", notice=0)
    for day in range(7):
        await add_availability(engine, mentor, day_of_week=day)
    return mentor, slow, quick


async def stored(engine: AsyncEngine, mentor: UUID) -> UUID | None:
    async with engine.begin() as conn:
        value: UUID | None = (
            await conn.execute(
                text(
                    "SELECT next_available_session_type_id FROM mentor_next_availability "
                    "WHERE mentor_user_id = :m"
                ),
                {"m": mentor},
            )
        ).scalar_one()
    return value


async def test_the_job_stores_the_offering_of_the_earliest_slot(db_engine: AsyncEngine) -> None:
    mentor, _slow, quick = await two_offerings(db_engine, "type-job")

    await refresh(db_engine)

    assert await stored(db_engine, mentor) == quick


async def test_the_card_and_the_profile_send_it_while_open(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, _slow, quick = await two_offerings(db_engine, "type-open")
    await refresh(db_engine)

    row = await card(api_client, mentor)
    profile = (await api_client.get(f"/api/v1/mentors/{mentor}")).json()

    assert row["next_available_state"] == "open"
    assert row["next_available_session_type_id"] == str(quick)
    assert profile["next_available_session_type_id"] == str(quick)
    # One of the offerings the profile lists, so the client can open it.
    assert str(quick) in {s["id"] for s in profile["session_types"]}


async def test_nothing_free_stores_no_offering(db_engine: AsyncEngine) -> None:
    from tests.integration.test_mentor_next_available import FakeCalendar

    from app.domain.availability import UtcInterval

    mentor, _slow, _quick = await two_offerings(db_engine, "type-none")
    now = dt.datetime.now(dt.UTC)
    booked = FakeCalendar(
        (UtcInterval(start=now - dt.timedelta(days=2), end=now + dt.timedelta(days=90)),)
    )

    await refresh(db_engine, now=now, calendar=booked)

    assert await stored(db_engine, mentor) is None


async def test_a_retired_offering_is_not_sent(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Switching the offering off logs a change, so the state is `refreshing`
    and the id is withheld with the time — never an offering the client could
    not open."""
    mentor, _slow, quick = await two_offerings(db_engine, "type-retired")
    await refresh(db_engine)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE session_types SET is_active = false WHERE id = :t"), {"t": quick}
        )

    row = await card(api_client, mentor)

    assert row["next_available_state"] == "refreshing"
    assert row["next_available_session_type_id"] is None


async def test_a_deleted_offering_leaves_the_row_standing(db_engine: AsyncEngine) -> None:
    """`ON DELETE SET NULL`: a hard-deleted offering must not take the mentor's
    cached row with it or be refused; the next refresh rewrites the value."""
    mentor, _slow, quick = await two_offerings(db_engine, "type-deleted")
    await refresh(db_engine)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM session_type_booking_configs WHERE session_type_id = :t"),
            {"t": quick},
        )
        await conn.execute(text("DELETE FROM session_types WHERE id = :t"), {"t": quick})

    assert await stored(db_engine, mentor) is None
