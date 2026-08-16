"""Education, scholarships and languages on the public mentor profile.

Three lists on `GET /mentors/{handle}`, read by the **same** store functions that
serve the owner-facing endpoints — narrowed at the schema, never re-queried.
That is decision #94's rule, and it is why the interesting tests here are about
what is *absent*: the owner's view of an award carries `evidence_url`, a link to
their proof document, and this endpoint takes no token at all.

**Two of these tests exist because of what the confidence check found**, not
because of the design. `list_education` ordered by `is_most_recent` — blank on
every migrated row, so the ordering was a no-op that read as a rule — and it
returned no abbreviation, so this page would have rendered "Doctorate (PhD)"
beside a card rendering "Ph.D".
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import add_education, make_bookable_mentor

pytestmark = [pytest.mark.db, pytest.mark.anyio]

URL = "/api/v1/mentors"


async def profile(client: httpx.AsyncClient, mentor: object) -> dict:
    response = await client.get(f"{URL}/{mentor}")
    assert response.status_code == 200, response.text
    return response.json()


async def add_award(
    engine: AsyncEngine,
    user: object,
    *,
    title: str = "Chevening",
    institution: str = "Oxford",
    year: int | None = 2024,
    evidence_url: str | None = "https://private.example/proof.pdf",
    deleted: bool = False,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_awards "
                "(user_id, title, institution, year, evidence_url, deleted_at) "
                "VALUES (:u, :t, :i, :y, :e, CASE WHEN :d THEN now() END)"
            ),
            {"u": user, "t": title, "i": institution, "y": year, "e": evidence_url, "d": deleted},
        )


async def add_language(engine: AsyncEngine, user: object, display_name: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_languages (user_id, language_id) "
                "SELECT :u, id FROM languages WHERE display_name = :n"
            ),
            {"u": user, "n": display_name},
        )


# --------------------------------------------------------------------------
# The three lists
# --------------------------------------------------------------------------


async def test_the_profile_carries_education_scholarships_and_languages(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "sections")
    await add_education(db_engine, mentor, level="doctorate", abbreviation="Ph.D")
    await add_award(db_engine, mentor)
    await add_language(db_engine, mentor, "French")

    body = await profile(api_client, mentor)

    assert body["education"][0]["degree"] == "Ph.D"
    assert body["education"][0]["study_course"] == "Mathematics"
    assert body["education"][0]["institution"] == "Washington University"
    assert body["scholarships"][0]["title"] == "Chevening"
    assert body["scholarships"][0]["institution"] == "Oxford"
    assert body["scholarships"][0]["year"] == 2024
    assert body["languages"][0]["display_name"] == "French"


async def test_a_mentor_with_none_of_them_still_has_a_profile(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Empty lists, never null, never a 404.

    Languages are the ordinary case rather than the edge: the legacy export
    carries one for 3 profiles in 19, so most migrated mentors have none.
    """
    mentor = await make_bookable_mentor(db_engine, "sections-empty")

    body = await profile(api_client, mentor)

    assert body["education"] == []
    assert body["scholarships"] == []
    assert body["languages"] == []


# --------------------------------------------------------------------------
# What must not leave the building
# --------------------------------------------------------------------------


async def test_the_award_evidence_link_is_not_public(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The one that matters.

    `list_awards` returns `evidence_url` because the owner needs it. It is a link
    to the holder's proof document, and this endpoint takes no token — so reusing
    that projection verbatim would publish it. Asserted against the whole
    serialised body rather than the award object, because a field can leak
    through a nested shape nobody thought to check.
    """
    mentor = await make_bookable_mentor(db_engine, "sections-evidence")
    await add_award(db_engine, mentor, evidence_url="https://private.example/secret-proof.pdf")

    response = await api_client.get(f"{URL}/{mentor}")

    assert "secret-proof" not in response.text
    assert "evidence_url" not in response.text


async def test_the_owner_facing_fields_stay_out_of_the_public_shape(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Each name is listed because each is returned by a store function reused here.

    `verification_status` is `unverified` on every row, since nothing verifies an
    award — publishing it would say something untrue about the holder rather than
    something true about the platform. `degree_category` is the raw legacy value
    the field mapping marks *migrate, then deprecate*. `is_most_recent` is blank
    everywhere and means nothing (D98). `legacy_bubble_id` never leaves, ever.

    **Asserted on the key sets, not on the serialised text.** The first version
    of this test searched the response body for each name and failed on
    `study_program` — which is a substring of `primary_study_program`, a
    legitimate top-level field describing the mentor rather than a leaked
    education column. A substring check over JSON reports fields that are not
    there, and would equally have missed one nested somewhere it did not look.
    An exact key set says what the shape *is*, so a field added later has to be
    added here too.
    """
    mentor = await make_bookable_mentor(db_engine, "sections-allowlist")
    await add_education(db_engine, mentor, level="masters")
    await add_award(db_engine, mentor)
    await add_language(db_engine, mentor, "French")

    body = await profile(api_client, mentor)

    assert set(body["education"][0]) == {
        "id",
        "degree",
        "study_course",
        "institution",
        "date_start",
        "date_end",
    }
    assert set(body["scholarships"][0]) == {"id", "title", "institution", "year"}
    assert set(body["languages"][0]) == {"id", "display_name", "code"}


async def test_the_owner_facing_endpoint_keeps_its_fields(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Narrowing the public view must not narrow the owner's.

    The two share one query and differ only in projection, which is the whole
    design — and the failure mode of that design is tightening the shared half.
    """
    from uuid import uuid4

    from conftest import api_token, bearer

    auth_id = uuid4()
    mentor = await make_bookable_mentor(db_engine, "sections-owner")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET auth_id = :a WHERE id = :u"), {"a": auth_id, "u": mentor}
        )
    await add_award(db_engine, mentor)

    owner = await api_client.get(
        f"/api/v1/users/{mentor}/awards", headers=bearer(api_token(auth_id))
    )

    assert owner.status_code == 200, owner.text
    assert owner.json()["data"][0]["evidence_url"] is not None


# --------------------------------------------------------------------------
# Ordering, and the soft delete
# --------------------------------------------------------------------------


async def test_education_is_newest_first_without_relying_on_a_dead_flag(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`is_most_recent` is false on every row, so it can decide nothing.

    The fixture leaves it false everywhere — which is what the migrated data
    looks like — so an ordering that depended on it would return these in
    insertion order and this test would catch it.
    """
    mentor = await make_bookable_mentor(db_engine, "sections-order")
    await add_education(
        db_engine, mentor, level="bachelors", course="Oldest", date_end="2015-01-01"
    )
    await add_education(
        db_engine, mentor, level="doctorate", course="Newest", date_end="2026-01-01"
    )
    await add_education(db_engine, mentor, level="masters", course="Middle", date_end="2020-01-01")

    body = await profile(api_client, mentor)

    assert [e["study_course"] for e in body["education"]] == ["Newest", "Middle", "Oldest"]


async def test_a_dateless_entry_does_not_float_to_the_top(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`nulls_last`, and PostgreSQL sorts nulls first under `DESC` without it."""
    mentor = await make_bookable_mentor(db_engine, "sections-nulldate")
    await add_education(db_engine, mentor, course="Dated", date_end="2024-01-01")
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO education_entries (user_id, school_name_raw, study_course) "
                "VALUES (:u, 'Nowhere', 'Undated')"
            ),
            {"u": mentor},
        )

    body = await profile(api_client, mentor)

    assert body["education"][0]["study_course"] == "Dated"


async def test_awards_are_newest_first(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "sections-awardorder")
    await add_award(db_engine, mentor, title="Older", year=2019)
    await add_award(db_engine, mentor, title="Newer", year=2025)

    body = await profile(api_client, mentor)

    assert [a["title"] for a in body["scholarships"]] == ["Newer", "Older"]


async def test_soft_deleted_rows_are_absent_from_both_lists(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`education_entries` and `user_awards` both carry `deleted_at`.

    The store's own docstring records that this predicate has been missed twice
    in this repository, once on `user_awards` specifically.
    """
    mentor = await make_bookable_mentor(db_engine, "sections-deleted")
    await add_education(db_engine, mentor, course="Gone", deleted=True)
    await add_education(db_engine, mentor, course="Kept")
    await add_award(db_engine, mentor, title="Gone", deleted=True)
    await add_award(db_engine, mentor, title="Kept")

    body = await profile(api_client, mentor)

    assert [e["study_course"] for e in body["education"]] == ["Kept"]
    assert [a["title"] for a in body["scholarships"]] == ["Kept"]


# --------------------------------------------------------------------------
# One page, one product
# --------------------------------------------------------------------------


async def test_the_card_and_the_profile_spell_the_degree_the_same_way(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The reason `degree_abbreviation` was added to the shared statement.

    Before this, the card resolved `COALESCE(degree_abbreviation, short_name)` and
    the profile rendered `degree_level_name` — so one page would have shown
    "Ph.D" on the card and "Doctorate (PhD)" in the education list, from one row.
    """
    mentor = await make_bookable_mentor(db_engine, "sections-samespelling")
    await add_education(db_engine, mentor, level="doctorate", abbreviation="Ph.D")

    card = (await api_client.get(URL)).json()["data"][0]
    body = await profile(api_client, mentor)

    assert card["degree"] == body["education"][0]["degree"] == "Ph.D"


async def test_several_rows_of_everything_return_one_profile(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Three lists on one response is three chances for a join to fan out."""
    mentor = await make_bookable_mentor(db_engine, "sections-fanout")
    for index in range(3):
        await add_education(db_engine, mentor, course=f"Course {index}")
        await add_award(db_engine, mentor, title=f"Award {index}", year=2020 + index)
    for name in ("French", "Spanish"):
        await add_language(db_engine, mentor, name)

    body = await profile(api_client, mentor)

    assert len(body["education"]) == 3
    assert len(body["scholarships"]) == 3
    assert len(body["languages"]) == 2
    assert body["id"] == str(mentor)


# --------------------------------------------------------------------------
# Cases the mutation batch proved the fixtures above could not distinguish
# --------------------------------------------------------------------------


async def test_the_holder_s_abbreviation_beats_the_level_name_here_too(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A bachelor's, because a doctorate cannot tell the two rules apart.

    `degree_levels.short_name` for `doctorate` is "Ph.D" — the same string a
    holder would type — so every earlier test in this file passes whether the
    profile reads the holder's abbreviation or the level's default. `LL.B`
    against `Bachelor's` is the case where they differ, and a mutation dropping
    the holder's own value survived until this existed.
    """
    mentor = await make_bookable_mentor(db_engine, "sections-llb")
    await add_education(db_engine, mentor, level="bachelors", abbreviation="LL.B", course="Law")

    body = await profile(api_client, mentor)

    assert body["education"][0]["degree"] == "LL.B"


async def test_an_unmatched_school_shows_what_the_user_typed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """ADR 0008's pair, on the profile as well as the card.

    Every other fixture here links an institution, so the `COALESCE` fallback was
    never exercised and a mutation removing it survived.
    """
    mentor = await make_bookable_mentor(db_engine, "sections-rawschool")
    await add_education(db_engine, mentor, school="Unlisted Polytechnic", institution=False)

    body = await profile(api_client, mentor)

    assert body["education"][0]["institution"] == "Unlisted Polytechnic"


async def test_languages_are_alphabetical_rather_than_inserted(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Inserted out of order on purpose.

    Counting them proved nothing about the ordering, which is how a mutation
    swapping the sort key for insertion order survived.
    """
    mentor = await make_bookable_mentor(db_engine, "sections-langorder")
    for name in ("Spanish", "French", "German"):
        await add_language(db_engine, mentor, name)

    body = await profile(api_client, mentor)

    assert [x["display_name"] for x in body["languages"]] == ["French", "German", "Spanish"]


async def test_one_mentor_s_languages_do_not_appear_on_another_s_profile(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The scope predicate, which nothing else here could see.

    Every earlier fixture gives languages to exactly one mentor, so a query with
    no `user_id` filter returns the same rows and every assertion still holds.
    Two mentors is the whole test.
    """
    mine = await make_bookable_mentor(db_engine, "sections-mine")
    theirs = await make_bookable_mentor(db_engine, "sections-theirs")
    await add_language(db_engine, mine, "French")
    await add_language(db_engine, theirs, "Spanish")

    body = await profile(api_client, mine)

    assert [x["display_name"] for x in body["languages"]] == ["French"]
