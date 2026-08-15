"""What a discovery card actually renders, beyond a name and a headline.

The card is `Ph.D, Mathematics, Washington University` plus a completed-session
count, and until now `GET /mentors` could produce none of it. Three joins and an
aggregate, on both modes.

**Two of these tests exist because the first version of this feature was
specified against assumptions and the export disagreed.** `is_most_recent` is
blank on all 21 dev-export rows, so a card keyed on it renders nothing for
everybody; and the field a mentee reads as "Mathematics" is `study_course`, not
`study_program` — which holds degree *names* like `BSc (Bachelor of Science)` and
is populated on 8 rows of 21. Both would have shipped a blank line past a green
suite, because a fixture written from the same assumption agrees with it.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import (
    add_completed_sessions,
    add_education,
    make_bookable_mentor,
)

pytestmark = [pytest.mark.db, pytest.mark.anyio]

URL = "/api/v1/mentors"


async def card(client: httpx.AsyncClient, query: str = "") -> dict:
    """The first row of whichever mode is asked for.

    Both modes take the same assertions throughout this file: browse and search
    read one `_base()`, and a field present on one and absent from the other is
    the failure this shares a helper to catch.
    """
    response = await client.get(f"{URL}{query}")
    assert response.status_code == 200, response.text
    rows = response.json()["data"]
    assert rows, f"no mentor returned for {query!r}"
    return rows[0]


# --------------------------------------------------------------------------
# The academic line
# --------------------------------------------------------------------------


@pytest.mark.parametrize("query", ["", "?q=Lovelace"])
async def test_the_card_carries_degree_course_and_institution(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, query: str
) -> None:
    await make_bookable_mentor(db_engine, "card-line")
    mentor = await _only_mentor(db_engine)
    await add_education(db_engine, mentor, level="doctorate", abbreviation="Ph.D")

    row = await card(api_client, query)

    assert row["degree"] == "Ph.D"
    assert row["study_course"] == "Mathematics"
    assert row["institution"] == "Washington University"


async def test_a_mentor_without_education_still_appears(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Degrade, never exclude. Excluding on a *display* field is a different
    thing from excluding on a bookability rule, and an `INNER` join here would
    look identical in every test whose fixture has an education entry."""
    await make_bookable_mentor(db_engine, "card-none")

    row = await card(api_client)

    assert row["degree"] is None
    assert row["study_course"] is None
    assert row["institution"] is None
    assert row["completed_sessions"] == 0


async def test_the_abbreviation_the_user_holds_beats_the_level_default(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    await make_bookable_mentor(db_engine, "card-abbrev")
    mentor = await _only_mentor(db_engine)
    await add_education(db_engine, mentor, level="bachelors", abbreviation="LL.B")

    assert (await card(api_client))["degree"] == "LL.B"


async def test_no_abbreviation_inherits_the_generic_level_name(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`Bachelor's`, never `B.Sc`.

    This is the whole reason the fallback is not a member of the menu: guessing
    a specific abbreviation renders `B.Sc, Law` for a law graduate, and the ISCED
    migration reshaped this table precisely because a Nigerian BSc, a UK BA and a
    US Bachelor's are one level and three words.
    """
    await make_bookable_mentor(db_engine, "card-inherit")
    mentor = await _only_mentor(db_engine)
    await add_education(db_engine, mentor, level="bachelors", abbreviation=None, course="Law")

    assert (await card(api_client))["degree"] == "Bachelor's"


async def test_an_unmatched_school_falls_back_to_what_the_user_typed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`institution_id` is nullable and `school_name_raw` is not — ADR 0008's pair."""
    await make_bookable_mentor(db_engine, "card-raw")
    mentor = await _only_mentor(db_engine)
    await add_education(db_engine, mentor, school="Somewhere Unlisted", institution=False)

    assert (await card(api_client))["institution"] == "Somewhere Unlisted"


# --------------------------------------------------------------------------
# Which entry, when there are several
# --------------------------------------------------------------------------


async def test_several_entries_yield_one_row_and_the_highest_qualification(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Two things at once, on purpose.

    **One row** — the join must not multiply the page. And **the right row**: the
    bachelor's here ends *later* than the doctorate, so a rule ordering by date
    alone picks the wrong one, and a mentor taking a second degree after a PhD
    would stop showing the PhD.
    """
    await make_bookable_mentor(db_engine, "card-many")
    mentor = await _only_mentor(db_engine)
    await add_education(
        db_engine, mentor, level="doctorate", abbreviation="Ph.D", date_end="2020-01-01"
    )
    await add_education(
        db_engine,
        mentor,
        level="bachelors",
        abbreviation="B.Sc",
        course="Basket Weaving",
        date_end="2026-01-01",
    )

    response = await api_client.get(URL)
    rows = response.json()["data"]

    assert len(rows) == 1, "the education join multiplied the page"
    assert rows[0]["degree"] == "Ph.D"
    assert rows[0]["study_course"] == "Mathematics"


async def test_the_later_of_two_equal_levels_wins(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`date_end` is the tiebreak once the level cannot decide."""
    await make_bookable_mentor(db_engine, "card-tie")
    mentor = await _only_mentor(db_engine)
    await add_education(db_engine, mentor, level="masters", course="Older", date_end="2019-01-01")
    await add_education(db_engine, mentor, level="masters", course="Newer", date_end="2025-01-01")

    assert (await card(api_client))["study_course"] == "Newer"


async def test_a_deleted_entry_is_not_the_one_shown(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The sixth soft delete of this milestone, in the seventh place it matters."""
    await make_bookable_mentor(db_engine, "card-deleted")
    mentor = await _only_mentor(db_engine)
    await add_education(db_engine, mentor, level="doctorate", course="Deleted", deleted=True)
    await add_education(db_engine, mentor, level="masters", course="Live")

    assert (await card(api_client))["study_course"] == "Live"


# --------------------------------------------------------------------------
# The completed count
# --------------------------------------------------------------------------


@pytest.mark.parametrize("query", ["", "?q=Lovelace"])
async def test_completed_sessions_are_counted(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, query: str
) -> None:
    await make_bookable_mentor(db_engine, "card-count")
    mentor = await _only_mentor(db_engine)
    await add_completed_sessions(db_engine, mentor, 3)

    assert (await card(api_client, query))["completed_sessions"] == 3


@pytest.mark.parametrize("status", ["cancelled", "declined", "expired", "no_show", "confirmed"])
async def test_only_completed_sessions_count(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, status: str
) -> None:
    """`no_show` is deliberately excluded and is the interesting one.

    A session where the mentee never arrived held the mentor's time and delivered
    nothing, so it is not a completed session — and it is not lost either: it has
    its own status, and per-party attendance lives on `session_participants`,
    which is what the profile's separate attendance figure will read.
    """
    await make_bookable_mentor(db_engine, f"card-{status}")
    mentor = await _only_mentor(db_engine)
    await add_completed_sessions(db_engine, mentor, 2, status=status)

    assert (await card(api_client))["completed_sessions"] == 0


async def test_the_count_is_zero_rather_than_null(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Zero is a real answer. `null` would make every client write the same
    coalesce, and a card cannot render "no data" differently from "none yet"."""
    await make_bookable_mentor(db_engine, "card-zero")

    assert (await card(api_client))["completed_sessions"] == 0


async def test_the_count_is_the_mentor_s_own_not_their_mentee_side(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A mentor is also somebody's mentee. Sessions they attended as the mentee
    are not sessions they delivered, and `mentee_id` is a different column."""
    await make_bookable_mentor(db_engine, "card-side")
    mentor = await _only_mentor(db_engine)
    await add_completed_sessions(db_engine, mentor, 1)
    async with db_engine.begin() as conn:
        await conn.execute(text("UPDATE sessions SET mentee_id = mentor_id, mentor_id = mentee_id"))

    assert (await card(api_client))["completed_sessions"] == 0


async def _only_mentor(engine: AsyncEngine) -> object:
    async with engine.begin() as conn:
        return (
            await conn.execute(text("SELECT user_id FROM mentor_profiles ORDER BY id DESC LIMIT 1"))
        ).scalar_one()


# --------------------------------------------------------------------------
# The catalogue a client picks an abbreviation from
# --------------------------------------------------------------------------


async def test_the_degree_catalogue_serves_the_menu_and_the_fallback(
    api_client: httpx.AsyncClient,
) -> None:
    """`short_forms` must arrive as a JSON array, not a Postgres literal.

    `LookupRead` had never carried an array before this, and `'{B.Sc,B.A}'`
    reaching a client as a string would satisfy every type annotation on the way
    out while being unusable at the other end.
    """
    response = await api_client.get("/api/v1/catalog/degree-levels")

    assert response.status_code == 200, response.text
    levels = {row["code"]: row for row in response.json()["data"]}
    assert levels["bachelors"]["short_name"] == "Bachelor's"
    assert isinstance(levels["bachelors"]["short_forms"], list)
    assert "LL.B" in levels["bachelors"]["short_forms"]
    assert levels["doctorate"]["short_name"] == "Ph.D"


async def test_the_fallback_is_not_a_member_of_the_menu(
    api_client: httpx.AsyncClient,
) -> None:
    """Only where the level's abbreviations actually vary by field.

    **The first version of this test asserted "never a member" and was wrong**,
    which is the useful part. `Ph.D` and `Diploma` are each simultaneously the
    generic name for their level *and* a specific abbreviation somebody holds, so
    both legitimately appear in their own menu.

    The rule that does hold is narrower: where a level covers many field-specific
    abbreviations — a bachelor's is `B.Sc` or `B.A` or `LL.B` depending entirely
    on what was studied — the fallback must be none of them. Picking one renders
    "B.Sc, Law", and that is the failure the generic label exists to prevent.
    """
    response = await api_client.get("/api/v1/catalog/degree-levels")
    levels = {row["code"]: row for row in response.json()["data"]}

    for code in ("bachelors", "masters"):
        assert levels[code]["short_name"] not in levels[code]["short_forms"], code
        assert len(levels[code]["short_forms"]) > 2, f"{code}: a menu of one is a default"


async def test_every_stored_abbreviation_is_offered_by_its_level(
    db_engine: AsyncEngine,
) -> None:
    """A menu that cannot show a user the value they already hold is broken.

    Asserted against the database rather than the export, so it keeps holding as
    rows are written by the API rather than only by the migration.
    """
    await make_bookable_mentor(db_engine, "menu-check")
    mentor = await _only_mentor(db_engine)
    await add_education(db_engine, mentor, level="bachelors", abbreviation="LL.B")

    async with db_engine.begin() as conn:
        stray = (
            await conn.execute(
                text(
                    "SELECT e.degree_abbreviation, dl.slug FROM education_entries e "
                    "JOIN degree_levels dl ON dl.id = e.degree_level_id "
                    "WHERE e.degree_abbreviation IS NOT NULL "
                    "  AND NOT (e.degree_abbreviation = ANY(dl.short_forms))"
                )
            )
        ).all()

    assert stray == [], f"held but not offered: {stray}"


# --------------------------------------------------------------------------
# The plan, not the assumption
# --------------------------------------------------------------------------


async def test_the_completed_count_uses_its_partial_index(db_engine: AsyncEngine) -> None:
    """`ix_sessions_mentor_completed` existed before any reader did.

    It is partial — `WHERE status = 'completed'` — and a partial index only
    serves a query whose predicate the planner can prove implies it. Phrase the
    count slightly differently and it silently seq-scans `sessions`, which is the
    table that grows fastest here. That is not hypothetical: an `EXISTS` defeated
    the partial slug index in #73 and cost 1.9ms against 0.046ms, and it was
    found by measuring rather than by reading the query.

    `enable_seqscan = off` because at test scale every plan is a sequential scan
    regardless — the assertion is about which index *can* serve this shape, not
    about what the planner picks over twelve rows.
    """
    async with db_engine.connect() as conn:
        await conn.execute(text("SET enable_seqscan = off"))
        plan = await conn.execute(
            text(
                "EXPLAIN SELECT count(*) FROM sessions "
                "WHERE mentor_id = '00000000-0000-0000-0000-000000000000'::uuid "
                "  AND status = 'completed'"
            )
        )
        rendered = "\n".join(row[0] for row in plan)

    assert "ix_sessions_mentor_completed" in rendered, rendered


# --------------------------------------------------------------------------
# The nullable link nothing else exercises
# --------------------------------------------------------------------------


async def test_an_entry_with_no_degree_level_still_renders_what_it_has(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`degree_level_id` is nullable and the write path permits it.

    A user may record a school and a course without saying what level it was, and
    the card must show what they did give rather than dropping the whole line.
    With no level there is no `short_name` to inherit, so `degree` is null unless
    they typed an abbreviation — which is exactly what `COALESCE` means here.
    """
    await make_bookable_mentor(db_engine, "card-nolevel")
    mentor = await _only_mentor(db_engine)
    await add_education(db_engine, mentor, level=None, course="Philosophy")

    row = await card(api_client)

    assert row["degree"] is None
    assert row["study_course"] == "Philosophy"
    assert row["institution"] == "Washington University"


async def test_a_levelled_entry_outranks_an_unlevelled_one(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`nullslast()` on the level, and it is load-bearing.

    Without it PostgreSQL sorts nulls *first* under `DESC`, so a user who
    recorded one school with no level would have that entry beat their doctorate
    — the card would show a blank degree for somebody who has one.
    """
    await make_bookable_mentor(db_engine, "card-nullorder")
    mentor = await _only_mentor(db_engine)
    await add_education(db_engine, mentor, level=None, course="Unlevelled")
    await add_education(db_engine, mentor, level="doctorate", abbreviation="Ph.D", course="Real")

    row = await card(api_client)

    assert row["degree"] == "Ph.D"
    assert row["study_course"] == "Real"
