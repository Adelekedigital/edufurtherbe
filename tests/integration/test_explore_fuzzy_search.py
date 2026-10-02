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
        "राम",
        "́",
        "́ ́x x́",
        "lovelace (-harvard",
        "x" * 200,
    ],
)
async def test_hostile_input_never_breaks_the_search(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, query: str
) -> None:
    await make_bookable_mentor(db_engine, "fz-hostile")

    response = await api_client.get(URL, params={"q": query})

    assert response.status_code == 200, response.text


async def test_a_negated_word_still_excludes_the_mentor(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`-Harvard` is an exclusion. The forgiving tiers must not read it as a
    positive prefix and OR everybody back in (Codex on #328)."""
    harvard = await make_bookable_mentor(db_engine, "fz-neg-harvard")
    await add_education(db_engine, harvard, school="Harvard University")
    other = await make_bookable_mentor(db_engine, "fz-neg-other")
    await add_education(db_engine, other, school="Oxford University")

    found = await ids(api_client, "Lovelace -Harvard")

    assert str(other) in found
    assert str(harvard) not in found


async def test_a_mentor_with_only_a_last_name_is_found_by_its_prefix(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Names are nullable. Joined with `+`, one null blanked both (Codex on #328)."""
    mentor = await make_bookable_mentor(db_engine, "fz-null-first")
    async with db_engine.begin() as conn:
        await conn.execute(text("UPDATE users SET first_name = NULL WHERE id = :u"), {"u": mentor})

    assert str(mentor) in await ids(api_client, "Lov")


async def test_a_stop_word_does_not_block_a_prefix_match_in_prose(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The bio is `english`, which dropped "at"; the prefix query must too
    (Codex on #328)."""
    mentor = await make_bookable_mentor(db_engine, "fz-stopword")
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_profiles (user_id, about_me) VALUES (:u, 'I studied at Harvard')"
            ),
            {"u": mentor},
        )

    assert str(mentor) in await ids(api_client, "at harv")


async def test_a_word_inside_another_word_is_not_a_near_spelling(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`word_similarity` scored "mark" 0.6 against "denmark"; strict scoring
    compares whole words and puts it at 0.3, under the floor."""
    mentor = await make_bookable_mentor(db_engine, "fz-denmark")
    await set_headline(db_engine, mentor, "Nigeria Denmark")

    assert str(mentor) not in await ids(api_client, "mark")


async def test_near_spellings_still_match_under_strict_scoring(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    oxford = await make_bookable_mentor(db_engine, "fz-strict-oxford")
    await set_headline(db_engine, oxford, "Oxforrd admissions")

    assert str(oxford) in await ids(api_client, "oxford")


async def test_a_phrase_spanning_fields_matches_and_outranks_a_weaker_prefix(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Each word is a prefix under both configurations, and the stop word "at"
    is dropped, so a course and a school in different fields both count.

    Created first, so it can only lead by rank: ties break on id descending.
    """
    strong = await make_bookable_mentor(db_engine, "fz-phrase-strong")
    await add_education(db_engine, strong, school="Oxford University", course="Chemistry")
    weak = await make_bookable_mentor(db_engine, "fz-phrase-weak")
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_profiles (user_id, about_me) "
                "VALUES (:u, 'Chemistryland near Oxfordshire')"
            ),
            {"u": weak},
        )

    found = await ids(api_client, "chemistry at oxford")

    assert str(strong) in found
    assert found.index(str(strong)) < found.index(str(weak))


async def test_an_inflected_word_beside_a_partial_one_matches_stemmed_prose(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The bio indexed "studied" as `studi`. A `simple` prefix "studying:*"
    cannot reach it; the `english` half of the term, `studi:*`, does."""
    mentor = await make_bookable_mentor(db_engine, "fz-inflected")
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_profiles (user_id, about_me) VALUES (:u, 'I studied at Harvard')"
            ),
            {"u": mentor},
        )

    assert str(mentor) in await ids(api_client, "studying harv")


async def test_every_word_of_a_near_spelling_must_be_near(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Joined into one string, a single close word scored 0.55 for both and let
    the absent one ride along (Codex on #328)."""
    mentor = await make_bookable_mentor(db_engine, "fz-all-terms")
    await set_headline(db_engine, mentor, "Scholarship mentor for engineers")

    assert str(mentor) not in await ids(api_client, "scholarship zebrafish")
    assert str(mentor) in await ids(api_client, "scholarshp enginers")


async def test_a_short_word_still_binds_a_near_spelling(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Too short to be near-matched, "MIT" must still be present as a prefix
    (Codex on #328)."""
    harvard_only = await make_bookable_mentor(db_engine, "fz-mit-no")
    await add_education(db_engine, harvard_only, school="Harvard University")
    both = await make_bookable_mentor(db_engine, "fz-mit-yes")
    await add_education(db_engine, both, school="Harvard University")
    await set_headline(db_engine, both, "MIT alumni mentor")

    found = await ids(api_client, "MIT Harvrd")

    assert str(both) in found
    assert str(harvard_only) not in found


async def test_a_stop_word_does_not_block_a_near_spelling(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "fz-near-stop")
    await add_education(db_engine, mentor, school="Oxford University", course="Chemistry")

    assert str(mentor) in await ids(api_client, "chemistry at oxforrd")


async def test_or_keeps_typo_tolerance_on_each_side(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`OR` is a disjunction to the exact parser; the forgiving tiers must not
    AND it away (Codex on #328)."""
    harvard = await make_bookable_mentor(db_engine, "fz-or-harvard")
    await add_education(db_engine, harvard, school="Harvard University")
    oxford = await make_bookable_mentor(db_engine, "fz-or-oxford")
    await add_education(db_engine, oxford, school="Oxford University")

    found = await ids(api_client, "Harvrd OR Oxford")

    assert str(harvard) in found
    assert str(oxford) in found


async def test_a_long_stop_word_does_not_block_a_near_spelling(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """ "with" is long enough to be near-matched but is an english stop word,
    which the prefix tier already drops (Codex on #328)."""
    mentor = await make_bookable_mentor(db_engine, "fz-near-long-stop")
    await add_education(db_engine, mentor, school="Oxford University", course="Chemistry")

    assert str(mentor) in await ids(api_client, "chemistry with oxforrd")


async def test_stop_words_alone_do_not_match_everyone(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    await make_bookable_mentor(db_engine, "fz-only-stops")

    assert await ids(api_client, "with about") == []
