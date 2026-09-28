"""`joined_at` and `top_award` on the mentor card and profile (frontend #14, #17b).

`joined_at` is when the person became a mentor — `mentor_profiles.created_at`,
backfilled from the legacy platform for migrated mentors. `top_award` is the
title the profile's own award list puts first: the same predicate and the same
order, so the card can never name an award the profile would not lead with.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import make_bookable_mentor

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def add_award(
    engine: AsyncEngine,
    mentor: UUID,
    title: str,
    *,
    year: int | None = None,
    deleted: bool = False,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_awards (user_id, institution, title, year, deleted_at) "
                "VALUES (:u, 'Somewhere', :t, :y, CASE WHEN :d THEN now() END)"
            ),
            {"u": mentor, "t": title, "y": year, "d": deleted},
        )


async def set_mentor_since(engine: AsyncEngine, mentor: UUID, when: dt.datetime) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE mentor_profiles SET created_at = :w WHERE user_id = :u"),
            {"u": mentor, "w": when},
        )


async def card_of(client: httpx.AsyncClient, mentor: UUID) -> dict:
    rows = (await client.get("/api/v1/mentors?limit=50")).json()["data"]
    return next(row for row in rows if row["id"] == str(mentor))


async def profile_of(client: httpx.AsyncClient, mentor: UUID) -> dict:
    return dict((await client.get(f"/api/v1/mentors/{mentor}")).json())


SINCE = dt.datetime(2024, 3, 5, 10, 0, tzinfo=dt.UTC)


async def test_the_card_says_when_they_became_a_mentor(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "joined-card")
    await set_mentor_since(db_engine, mentor, SINCE)

    card = await card_of(api_client, mentor)

    assert dt.datetime.fromisoformat(card["joined_at"]) == SINCE


async def test_the_profile_says_when_they_became_a_mentor(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "joined-profile")
    await set_mentor_since(db_engine, mentor, SINCE)

    profile = await profile_of(api_client, mentor)

    assert dt.datetime.fromisoformat(profile["joined_at"]) == SINCE


async def test_the_card_names_the_most_recent_award(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "award-recent")
    await add_award(db_engine, mentor, "Older Award", year=2019)
    await add_award(db_engine, mentor, "Chevening Scholarship", year=2023)
    await add_award(db_engine, mentor, "Undated Prize")

    card = await card_of(api_client, mentor)

    assert card["top_award"] == "Chevening Scholarship"


async def test_the_card_leads_with_what_the_profile_leads_with(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """One order, not two: same-year ties break the way the profile's list does."""
    mentor = await make_bookable_mentor(db_engine, "award-tie")
    await add_award(db_engine, mentor, "Zeta Fellowship", year=2022)
    await add_award(db_engine, mentor, "Alpha Grant", year=2022)
    await add_award(db_engine, mentor, "Undated Prize")

    card = await card_of(api_client, mentor)
    profile = await profile_of(api_client, mentor)

    assert card["top_award"] == profile["scholarships"][0]["title"]
    assert profile["top_award"] == card["top_award"]
    # And the tie itself breaks alphabetically, as the profile lists them.
    assert card["top_award"] == "Alpha Grant"


async def test_a_deleted_award_is_never_the_top_one(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "award-deleted")
    await add_award(db_engine, mentor, "Kept Award", year=2020)
    await add_award(db_engine, mentor, "Withdrawn Award", year=2024, deleted=True)

    card = await card_of(api_client, mentor)

    assert card["top_award"] == "Kept Award"


async def test_a_mentor_without_awards_has_none(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "award-none")

    card = await card_of(api_client, mentor)
    profile = await profile_of(api_client, mentor)

    assert card["top_award"] is None
    assert profile["top_award"] is None


async def test_another_mentors_award_is_not_borrowed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mine = await make_bookable_mentor(db_engine, "award-mine")
    theirs = await make_bookable_mentor(db_engine, "award-theirs")
    await add_award(db_engine, theirs, "Their Award", year=2024)

    assert (await card_of(api_client, mine))["top_award"] is None
    assert (await card_of(api_client, theirs))["top_award"] == "Their Award"


async def test_the_featured_card_carries_both(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Featured and similar reuse the card, so they get the fields for free."""
    mentor = await make_bookable_mentor(db_engine, "award-featured")
    await add_award(db_engine, mentor, "Featured Award", year=2021)

    body = (await api_client.get("/api/v1/featured-mentor")).json()

    assert body["top_award"] == "Featured Award"
    assert body["joined_at"] is not None
