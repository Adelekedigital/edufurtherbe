"""Explore search forgives a half-typed word and a typo (#227).

Full-text search matched whole words and their stems only, so "harv" or
"Harvrd" found nobody. A search now matches on three tiers — the exact word,
a prefix of it, or a near spelling — and ranks them in that order, after
bookable-first (#220).
"""

from __future__ import annotations

from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import (
    add_availability,
    add_education,
    make_bookable_mentor,
    make_public_mentor,
)

pytestmark = [pytest.mark.db, pytest.mark.anyio]

URL = "/api/v1/mentors"


async def ids(client: httpx.AsyncClient, query: str) -> list[str]:
    response = await client.get(URL, params={"q": query})
    assert response.status_code == 200, response.text
    return [row["id"] for row in response.json()["data"]]


async def set_headline(engine: AsyncEngine, mentor: UUID, headline: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE mentor_profiles SET headline = :h WHERE user_id = :u"),
            {"u": mentor, "h": headline},
        )


async def test_a_half_typed_school_finds_the_mentor(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "fz-prefix")
    await add_education(db_engine, mentor, school="Harvard University")

    assert str(mentor) in await ids(api_client, "harv")


async def test_a_half_typed_surname_finds_the_mentor(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "fz-prefix-name")

    assert str(mentor) in await ids(api_client, "Lovel")


async def test_three_letters_find_by_prefix_alone(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Below the near-spelling minimum, so only the prefix tier can match it —
    the test that fails if prefix matching is lost."""
    mentor = await make_bookable_mentor(db_engine, "fz-prefix-short")

    assert str(mentor) in await ids(api_client, "Lov")


async def test_a_misspelt_school_finds_the_mentor(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "fz-typo")
    await add_education(db_engine, mentor, school="Harvard University")

    assert str(mentor) in await ids(api_client, "Harvrd")


async def test_a_misspelt_headline_word_finds_the_mentor(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "fz-typo-headline")
    await set_headline(db_engine, mentor, "Scholarship mentor for engineers")

    assert str(mentor) in await ids(api_client, "Scholarshp")


async def test_an_unrelated_word_still_finds_nobody(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Fuzzy is not anything-goes: the near-spelling floor keeps noise out."""
    await make_bookable_mentor(db_engine, "fz-noise")

    assert await ids(api_client, "zebrafish") == []


async def test_an_exact_match_ranks_above_a_near_spelling(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    # Created first: ties break on `mentor_profiles.id DESC`, so the exact match
    # can only come out on top by genuinely outranking.
    exact = await make_bookable_mentor(db_engine, "fz-rank-exact")
    await set_headline(db_engine, exact, "Oxford admissions")
    near = await make_bookable_mentor(db_engine, "fz-rank-near")
    await set_headline(db_engine, near, "Oxforrd admissions")

    found = await ids(api_client, "Oxford")

    assert found.index(str(exact)) < found.index(str(near))


async def test_a_prefix_match_ranks_above_a_near_spelling(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    prefix = await make_bookable_mentor(db_engine, "fz-rank-prefix")
    await set_headline(db_engine, prefix, "Oxfordshire admissions")
    near = await make_bookable_mentor(db_engine, "fz-rank-near-2")
    await set_headline(db_engine, near, "Oxforrd admissions")

    found = await ids(api_client, "Oxford")

    assert found.index(str(prefix)) < found.index(str(near))


async def test_bookable_first_still_leads_a_fuzzy_search(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """#220 is the leading key: a bookable near-spelling beats an unbookable
    exact match."""
    idle = await make_public_mentor(db_engine, "fz-idle")
    await add_availability(db_engine, idle)
    await set_headline(db_engine, idle, "Oxford admissions")
    bookable = await make_bookable_mentor(db_engine, "fz-bookable")
    await set_headline(db_engine, bookable, "Oxforrd admissions")

    found = await ids(api_client, "Oxford")

    assert found.index(str(bookable)) < found.index(str(idle))


async def test_total_and_paging_agree_with_the_fuzzy_match_set(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    expected = set()
    for n, school in enumerate(("Harvard University", "Harvard College", "Harvrd Institute")):
        mentor = await make_bookable_mentor(db_engine, f"fz-page-{n}")
        await add_education(db_engine, mentor, school=school)
        expected.add(str(mentor))
    await make_bookable_mentor(db_engine, "fz-page-other")

    first = await api_client.get(URL, params={"q": "harvard", "limit": 1})
    assert first.status_code == 200, first.text
    total = first.json()["total"]
    seen = [row["id"] for row in first.json()["data"]]
    cursor = first.json()["next_cursor"]
    while cursor:
        page = await api_client.get(URL, params={"q": "harvard", "limit": 1, "cursor": cursor})
        seen.extend(row["id"] for row in page.json()["data"])
        cursor = page.json()["next_cursor"]

    assert len(seen) == len(set(seen)) == total
    assert set(seen) == expected


@pytest.mark.parametrize(
    "query",
    [
        "a & | ! ( :*",
        "harv:*",
        "'; DROP TABLE users; --",
        "\\",
        "%_%",
        "))) (((",
        "<-> !!",
        "Ñandú São Paulo",
        "x" * 200,
    ],
)
async def test_hostile_input_never_breaks_the_search(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, query: str
) -> None:
    await make_bookable_mentor(db_engine, "fz-hostile")

    response = await api_client.get(URL, params={"q": query})

    assert response.status_code in (200, 422), response.text
