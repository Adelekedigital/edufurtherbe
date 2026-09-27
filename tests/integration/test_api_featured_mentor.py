"""`GET /api/v1/featured-mentor` — one mentor a week, chosen and kept.

The weighting and the rotation rule are unit-tested in `tests/unit/test_featured.py`.
What is tested here is what only the database can show: the week's pick is
stored and stable, a full rotation happens across real weeks, a mentor who stops
being bookable is replaced rather than shown, and two first requests racing each
other agree.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.integration.factories import make_bookable_mentor, make_public_mentor

from app.infra.db.featured_store import current_featured

pytestmark = [pytest.mark.db, pytest.mark.anyio]

URL = "/api/v1/featured-mentor"
MONDAY = dt.datetime(2026, 9, 28, 9, tzinfo=dt.UTC)


async def featured_on(engine: AsyncEngine, now: dt.datetime) -> UUID | None:
    async with AsyncSession(engine) as session:
        return await current_featured(session, now=now)


async def weeks_rows(engine: AsyncEngine) -> list[tuple[dt.date, UUID]]:
    async with engine.begin() as conn:
        rows = await conn.execute(
            text("SELECT week_start, mentor_user_id FROM featured_mentors ORDER BY created_at")
        )
        return [(row.week_start, row.mentor_user_id) for row in rows]


async def test_nobody_bookable_is_null(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    await make_public_mentor(db_engine, "featured-not-set-up")

    response = await api_client.get(URL)

    assert response.status_code == 200
    assert response.json() is None


async def test_the_featured_mentor_is_a_card_with_their_bio(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "featured-card")
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_profiles (user_id, about_me) VALUES (:m, 'I mentor engineers.') "
                "ON CONFLICT (user_id) DO UPDATE SET about_me = excluded.about_me"
            ),
            {"m": mentor},
        )

    body = (await api_client.get(URL)).json()

    assert body["id"] == str(mentor)
    assert body["about_me"] == "I mentor engineers."
    assert body["next_available_state"] in {"open", "none", "refreshing"}
    assert "offerings" in body


async def test_the_week_s_pick_is_kept(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    for n in range(4):
        await make_bookable_mentor(db_engine, f"featured-stable-{n}")

    first = (await api_client.get(URL)).json()["id"]
    again = (await api_client.get(URL)).json()["id"]

    assert first == again
    assert len(await weeks_rows(db_engine)) == 1


async def test_everyone_is_featured_once_before_anyone_twice(db_engine: AsyncEngine) -> None:
    mentors = {await make_bookable_mentor(db_engine, f"featured-cycle-{n}") for n in range(3)}

    picks = [await featured_on(db_engine, MONDAY + dt.timedelta(weeks=w)) for w in range(3)]

    assert set(picks) == mentors


async def test_a_new_cycle_does_not_repeat_last_week(db_engine: AsyncEngine) -> None:
    for n in range(3):
        await make_bookable_mentor(db_engine, f"featured-turn-{n}")

    picks = [await featured_on(db_engine, MONDAY + dt.timedelta(weeks=w)) for w in range(4)]

    assert picks[3] != picks[2]


async def test_a_mentor_who_stops_being_bookable_is_replaced(db_engine: AsyncEngine) -> None:
    for n in range(3):
        await make_bookable_mentor(db_engine, f"featured-gone-{n}")
    first = await featured_on(db_engine, MONDAY)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE mentor_profiles SET listing_status = 'unlisted' WHERE user_id = :m"),
            {"m": first},
        )

    replacement = await featured_on(db_engine, MONDAY + dt.timedelta(hours=1))

    assert replacement is not None
    assert replacement != first


async def test_a_mentor_who_is_not_bookable_is_never_picked(db_engine: AsyncEngine) -> None:
    bookable = await make_bookable_mentor(db_engine, "featured-only-one")
    await make_public_mentor(db_engine, "featured-no-hours")

    picks = {await featured_on(db_engine, MONDAY + dt.timedelta(weeks=w)) for w in range(3)}

    assert picks == {bookable}


async def test_two_first_requests_agree(db_engine: AsyncEngine) -> None:
    """The week's first two requests arriving together must not each pick."""
    for n in range(5):
        await make_bookable_mentor(db_engine, f"featured-race-{n}")

    first, second = await asyncio.gather(
        featured_on(db_engine, MONDAY), featured_on(db_engine, MONDAY)
    )

    assert first == second
    assert len(await weeks_rows(db_engine)) == 1


async def test_the_second_rotation_is_also_complete(db_engine: AsyncEngine) -> None:
    """The cycle count has to advance: stuck on one cycle, the second rotation
    would only ever exclude last week's mentor, and could repeat anyone else."""
    mentors = {await make_bookable_mentor(db_engine, f"featured-second-{n}") for n in range(3)}

    picks = [await featured_on(db_engine, MONDAY + dt.timedelta(weeks=w)) for w in range(6)]

    assert set(picks[:3]) == mentors
    assert set(picks[3:]) == mentors
    # Asked of the history directly: a stuck counter can still produce a
    # complete-looking second rotation by chance, but never these numbers.
    async with db_engine.begin() as conn:
        cycles = (
            (await conn.execute(text("SELECT cycle FROM featured_mentors ORDER BY week_start")))
            .scalars()
            .all()
        )
    assert list(cycles) == [1, 1, 1, 2, 2, 2]
