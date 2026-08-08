"""Mirroring the catalogue into `institutions`, and linking education to it.

A synthetic catalogue of six records, never the real one. The real file is 2.25 MB
and lives behind a third party — a test that fetched it would be slow, would go
red on somebody else's outage, and would change meaning every two days as the
source moves.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.domain.institutions import to_catalogue_row
from app.infra.db.triggers import timestamps_from_source_across
from app.infra.etl.institutions import country_ids, link_education, mirror
from conftest import add_user

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

TABLES = ("institutions", "education_entries")
SYNCED = datetime(2026, 8, 8, 4, 0, tzinfo=UTC)


def catalogue() -> list[dict[str, Any]]:
    """Six records, each carrying one thing worth asserting."""
    return [
        {
            "name": "University of Lagos",
            "domains": ["unilag.edu.ng"],
            "web_pages": ["https://unilag.edu.ng/"],
            "alpha_two_code": "NG",
        },
        {
            "name": "University of Oxford",
            "domains": ["oxford.ac.uk"],
            "web_pages": ["https://ox.ac.uk/"],
            "alpha_two_code": "GB",
        },
        {"name": "No Page University", "domains": ["nopage.edu"], "alpha_two_code": "US"},
        # Two names, one domain — a real pair upstream (a merged art school, and
        # a college inside a university). `ON CONFLICT` collapses them.
        {"name": "National College of Art", "domains": ["khio.no"], "alpha_two_code": "NO"},
        {"name": "Oslo National Academy", "domains": ["khio.no"], "alpha_two_code": "NO"},
        # `XK` is a user-assigned code, not official ISO 3166-1, so `countries`
        # does not hold it. Five real records carry it.
        {"name": "American University in Kosovo", "domains": ["auk.org"], "alpha_two_code": "XK"},
        # One name, two institutions, two countries — a real collision. Upstream
        # has 73 of these over 158 records; `City University` is one of them.
        {"name": "City University", "domains": ["cityu.edu"], "alpha_two_code": "US"},
        {"name": "City University", "domains": ["city.ac.uk"], "alpha_two_code": "GB"},
    ]


async def run_mirror(conn: AsyncConnection, *, at: datetime = SYNCED, **overrides: Any) -> Any:
    rows = [to_catalogue_row(r) for r in overrides.get("records", catalogue())]
    countries = await country_ids(conn)
    async with timestamps_from_source_across(conn, TABLES):
        return await mirror(conn, rows, countries, synced_at=at)


async def add_education(conn: AsyncConnection, user_id: UUID, school: str) -> None:
    await conn.execute(
        text("INSERT INTO education_entries (user_id, school_name_raw) VALUES (:u, :s)"),
        {"u": user_id, "s": school},
    )


# --------------------------------------------------------------------------
# The mirror
# --------------------------------------------------------------------------


async def test_the_catalogue_lands_with_countries_resolved(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        counts = await run_mirror(conn)

        stored = await conn.execute(
            text(
                "SELECT i.name, c.code, i.source, i.last_synced_at FROM institutions i "
                "JOIN countries c ON c.id = i.country_id WHERE i.domain = 'unilag.edu.ng'"
            )
        )
        name, code, source, synced = stored.one()

    assert (name, code, source) == ("University of Lagos", "NG", "hipolabs")
    assert synced == SYNCED
    assert counts.seen == 8


async def test_an_unresolvable_country_is_skipped_and_named(db_engine: AsyncEngine) -> None:
    """Reported by code, never defaulted. A wrong country propagates into
    "who studied in the UK" and nothing would surface it."""
    async with db_engine.begin() as conn:
        counts = await run_mirror(conn)
        present = await conn.execute(
            text("SELECT count(*) FROM institutions WHERE domain = 'auk.org'")
        )

    assert counts.skipped_no_country == 1
    assert counts.unresolved_codes == ("XK",)
    assert present.scalar_one() == 0


async def test_two_names_on_one_domain_collapse_and_are_counted(
    db_engine: AsyncEngine,
) -> None:
    """Correct — one domain is one institution — but the count must say so, or
    the report claims more rows than the table holds."""
    async with db_engine.begin() as conn:
        counts = await run_mirror(conn)
        rows = await conn.execute(
            text("SELECT count(*) FROM institutions WHERE domain = 'khio.no'")
        )
        total = await conn.execute(text("SELECT count(*) FROM institutions"))

    assert counts.collapsed_domains == ("khio.no",)
    assert rows.scalar_one() == 1
    # 8 records, 1 collapsed before writing, 1 skipped for its country -> 6.
    # The two `City University` rows are two institutions on two domains, not a
    # collapse. `written` is now what reached the table, with nothing to subtract.
    assert (counts.written, total.scalar_one()) == (6, 6)


async def test_a_record_with_no_web_page_still_lands(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        await run_mirror(conn)
        page = await conn.execute(
            text("SELECT web_page FROM institutions WHERE domain = 'nopage.edu'")
        )

    assert page.scalar_one() is None


async def test_a_second_sync_stamps_but_does_not_churn_updated_at(
    db_engine: AsyncEngine,
) -> None:
    """The distinction the two columns exist for.

    `last_synced_at` must move on every sync — otherwise a week with no upstream
    changes is indistinguishable from a sync that stopped running, which is the
    ambiguity ADR 0008 raised against mirroring. `updated_at` must **not** move,
    because nothing about the row changed.
    """
    later = SYNCED + timedelta(days=7)
    async with db_engine.begin() as conn:
        await run_mirror(conn)
        before = await conn.execute(
            text("SELECT updated_at FROM institutions WHERE domain = 'unilag.edu.ng'")
        )
        first_updated = before.scalar_one()

    async with db_engine.begin() as conn:
        await run_mirror(conn, at=later)
        after = await conn.execute(
            text(
                "SELECT updated_at, last_synced_at FROM institutions WHERE domain = 'unilag.edu.ng'"
            )
        )
        updated, synced = after.one()
        total = await conn.execute(text("SELECT count(*) FROM institutions"))

    assert updated == first_updated, "updated_at moved on a row that did not change"
    assert synced == later, "last_synced_at did not move, so staleness is unknowable"
    assert total.scalar_one() == 6, "a re-sync duplicated rows"


async def test_a_collapsed_domain_does_not_churn_updated_at(
    db_engine: AsyncEngine,
) -> None:
    """The row two upstream records share must settle, not oscillate.

    `khio.no` arrives twice under different names. Written once each, the second
    hits `ON CONFLICT` and rewrites the row — so `updated_at` moves on every
    sync forever, while the stored content is identical every time. For that row
    `updated_at` would mean "a sync ran", which is exactly what `last_synced_at`
    was added to say.

    The sibling test asserts this for `unilag.edu.ng`, which appears once. That
    is why it passed while this was broken.
    """
    async with db_engine.begin() as conn:
        await run_mirror(conn)
        first = await conn.execute(
            text("SELECT updated_at FROM institutions WHERE domain = 'khio.no'")
        )
        before = first.scalar_one()

    async with db_engine.begin() as conn:
        await run_mirror(conn, at=SYNCED + timedelta(days=7))
        second = await conn.execute(
            text(
                "SELECT name, updated_at, last_synced_at FROM institutions WHERE domain = 'khio.no'"
            )
        )
        name, after, synced = second.one()

    assert after == before, "updated_at churned on a row whose content did not change"
    assert synced == SYNCED + timedelta(days=7), "last_synced_at must still move"
    # Deterministic, not whichever record happened to be written last.
    assert name == "National College of Art"


async def test_a_changed_name_does_move_updated_at(db_engine: AsyncEngine) -> None:
    """The other half. Without it the test above passes against a mirror that
    never touches `updated_at` at all."""
    renamed = [
        r | {"name": "Unilag"} if r["domains"] == ["unilag.edu.ng"] else r for r in catalogue()
    ]
    async with db_engine.begin() as conn:
        await run_mirror(conn)
        before = await conn.execute(
            text("SELECT updated_at FROM institutions WHERE domain = 'unilag.edu.ng'")
        )
        first = before.scalar_one()

    async with db_engine.begin() as conn:
        await run_mirror(conn, at=SYNCED + timedelta(days=7), records=renamed)
        after = await conn.execute(
            text("SELECT name, updated_at FROM institutions WHERE domain = 'unilag.edu.ng'")
        )
        name, updated = after.one()

    assert name == "Unilag"
    assert updated > first


# --------------------------------------------------------------------------
# The link
# --------------------------------------------------------------------------


async def test_an_exact_school_name_links(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        await run_mirror(conn)
        user = await add_user(conn, "grad@example.com")
        await add_education(conn, user, "University of Lagos")

        counts = await link_education(conn)
        linked = await conn.execute(
            text(
                "SELECT i.name, c.code FROM education_entries e "
                "JOIN institutions i ON i.id = e.institution_id "
                "JOIN countries c ON c.id = i.country_id"
            )
        )

    assert counts.linked == 1
    # The country the entry never asked for, derived from the institution.
    assert linked.one() == ("University of Lagos", "NG")


async def test_a_near_miss_does_not_link_and_keeps_its_raw_name(
    db_engine: AsyncEngine,
) -> None:
    """The failure that must stay visible. A wrong link would file this degree
    under a university the person never attended, and the study country would
    follow it."""
    async with db_engine.begin() as conn:
        await run_mirror(conn)
        user = await add_user(conn, "grad@example.com")
        await add_education(conn, user, "Univerity of Lagos")

        counts = await link_education(conn)
        row = await conn.execute(
            text("SELECT school_name_raw, institution_id FROM education_entries")
        )
        school, institution = row.one()

    assert counts.linked == 0
    assert counts.unmatched == ("Univerity of Lagos",)
    assert school == "Univerity of Lagos"
    assert institution is None


async def test_a_name_the_catalogue_holds_twice_is_reported_not_guessed(
    db_engine: AsyncEngine,
) -> None:
    """The silent-wrong-link case, end to end.

    Both rows are real institutions on real domains, so nothing upstream is
    wrong and no refusal fires. Linking either one would file this degree in the
    wrong country, permanently, with nothing to surface it — the exact failure
    the no-fuzzy-matching rule exists to prevent, arriving through the *exact*
    path. It has to be a question, not a guess.
    """
    async with db_engine.begin() as conn:
        await run_mirror(conn)
        user = await add_user(conn, "grad@example.com")
        await add_education(conn, user, "City University")

        counts = await link_education(conn)
        entry = await conn.execute(
            text("SELECT school_name_raw, institution_id FROM education_entries")
        )
        school, institution = entry.one()

    assert counts.linked == 0
    assert counts.ambiguous == ("City University",)
    # Reported apart from a miss: this needs somebody to say *which*, not
    # somebody to add a school the catalogue lacks.
    assert counts.unmatched == ()
    assert (school, institution) == ("City University", None)


async def test_the_link_pass_is_re_runnable(db_engine: AsyncEngine) -> None:
    """It is an UPDATE over rows that already exist, which is what lets matching
    be retuned without reloading anything (ADR 0020)."""
    async with db_engine.begin() as conn:
        await run_mirror(conn)
        user = await add_user(conn, "grad@example.com")
        await add_education(conn, user, "University of Lagos")
        await link_education(conn)

        second = await link_education(conn)
        linked = await conn.execute(
            text("SELECT count(*) FROM education_entries WHERE institution_id IS NOT NULL")
        )

    assert second.considered == 0, "an already-linked entry was reconsidered"
    assert linked.scalar_one() == 1


# --------------------------------------------------------------------------
# The nullable country
# --------------------------------------------------------------------------


async def test_a_manual_institution_needs_no_country(db_engine: AsyncEngine) -> None:
    """The schema change this PR makes, asserted directly.

    A user typing a school we do not hold creates a row an admin completes at
    review. `NOT NULL` would make that row unwriteable and push the work onto the
    user instead.
    """
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO institutions (name, source, status) "
                "VALUES ('Some Unlisted Polytechnic', 'manual', "
                "CAST('pending_review' AS lookup_status))"
            )
        )
        row = await conn.execute(
            text(
                "SELECT country_id, domain, last_synced_at FROM institutions "
                "WHERE source = 'manual'"
            )
        )

    # No country, no domain, and never seen by a sync — all three null, which is
    # what distinguishes a user-created row from a mirrored one.
    assert row.one() == (None, None, None)


async def test_several_manual_institutions_coexist_without_a_country(
    db_engine: AsyncEngine,
) -> None:
    """`domain` is UNIQUE and null for manual rows; PostgreSQL treats nulls as
    distinct, so pending duplicates from five people at one university coexist
    until an admin merges them."""
    async with db_engine.begin() as conn:
        for name in ("Polytechnic A", "Polytechnic B", "Polytechnic C"):
            await conn.execute(
                text("INSERT INTO institutions (name, source) VALUES (:n, 'manual')"),
                {"n": name},
            )
        count = await conn.execute(
            text("SELECT count(*) FROM institutions WHERE country_id IS NULL")
        )

    assert count.scalar_one() == 3
