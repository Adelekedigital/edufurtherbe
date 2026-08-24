"""Loading the reviews table, twice, with Bubble's timestamps intact.

**Twice is the whole point.** `trg_set_updated_at` is a `BEFORE UPDATE` trigger,
so a first load into an empty table preserves the source timestamps whether or
not the loader disables anything — a test that loads once and checks the values
passes against a loader with no `DISABLE` at all. It is the *re-run* that breaks:
the upsert takes its `DO UPDATE` branch, the trigger fires, and every migrated
`updated_at` becomes the import clock.

The ETL's recovery plan is a re-run and the runbook rehearses twice, so that is
the normal path rather than an edge case.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.transform.reviews import plan_reviews
from app.infra.etl.reconcile import reconcile_reviews
from app.infra.etl.reviews import ReviewLoader

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

NEW_YORK = ZoneInfo("America/New_York")
AUTHOR = "1734288518855x324438210652363800"
SUBJECT = "1734290858394x940262126235280600"


def record(**overrides: Any) -> dict[str, Any]:
    return {
        "unique id": "1755889805807x632569896287338500",
        "reviewedBy": AUTHOR,
        "reviewedFor": SUBJECT,
        "communicationRating": "5",
        "knowledgeRating": "3.34",
        "practicalityRating": "5",
        "supportRating": "3.34",
        "likertValuableRating": "4",
        "npsRecommendScore": "8",
        "publicReview": "He showed me what a strong portfolio looks like.",
        "privateReview": "Great platform experience",
        "Creation Date": "Aug 22, 2025 3:10 pm",
        "Modified Date": "Sep 3, 2025 5:31 am",
    } | overrides


@pytest_asyncio.fixture
async def users(db_engine: AsyncEngine) -> dict[str, UUID]:
    """The two parties, keyed by their Bubble anchors.

    Written straight to `users` rather than through `load_identity`: this file is
    about the reviews loader, and driving the identity phase to get two rows
    would make an identity bug look like a review bug.
    """
    tag = uuid4().hex[:8]
    resolved: dict[str, UUID] = {}
    async with db_engine.begin() as conn:
        for anchor, role in ((AUTHOR, "mentee"), (SUBJECT, "mentor")):
            user_id = (
                await conn.execute(
                    text(
                        "INSERT INTO users (email, primary_role, timezone, legacy_bubble_id) "
                        "VALUES (:e, :r, 'Africa/Lagos', :a) RETURNING id"
                    ),
                    {"e": f"{role}-{tag}@example.test", "r": role, "a": anchor},
                )
            ).scalar_one()
            resolved[anchor] = UUID(str(user_id))
    return resolved


async def load(engine: AsyncEngine, users: dict[str, UUID], records: list[dict[str, Any]]) -> Any:
    plan = plan_reviews(records, timezone=NEW_YORK)
    async with engine.begin() as conn:
        await ReviewLoader(conn).load(users=users, plan=plan)
        return await reconcile_reviews(conn, plan)


async def stored(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT legacy_bubble_id, session_id, reviewed_for_role, communication_rating, "
                "knowledge_rating, valuable_rating, nps_recommend_score, public_review, "
                "private_review, created_at, updated_at FROM reviews ORDER BY legacy_bubble_id"
            )
        )
        return [dict(row) for row in rows.mappings()]


# --------------------------------------------------------------------------
# One load
# --------------------------------------------------------------------------


async def test_a_review_lands_with_its_scale_mapped(
    db_engine: AsyncEngine, users: dict[str, UUID]
) -> None:
    await load(db_engine, users, [record()])

    row = (await stored(db_engine))[0]
    assert (row["communication_rating"], row["knowledge_rating"]) == (3, 2)
    assert (row["valuable_rating"], row["nps_recommend_score"]) == (4, 8)


async def test_a_migrated_review_carries_no_session(
    db_engine: AsyncEngine, users: dict[str, UUID]
) -> None:
    """Decision 5. The runbook says to match `reviewedBy` + `reviewedFor` +
    proximity; that has no anchor, and a fabricated link is irreversible where a
    later backfill is additive."""
    await load(db_engine, users, [record()])

    row = (await stored(db_engine))[0]
    assert row["session_id"] is None
    assert row["reviewed_for_role"] == "mentor"


async def test_the_source_timestamps_survive(
    db_engine: AsyncEngine, users: dict[str, UUID]
) -> None:
    """Bubble's clock, converted through the export's zone — not the import's."""
    await load(db_engine, users, [record()])

    row = (await stored(db_engine))[0]
    assert row["created_at"] == dt.datetime(2025, 8, 22, 19, 10, tzinfo=dt.UTC)
    assert row["updated_at"] == dt.datetime(2025, 9, 3, 9, 31, tzinfo=dt.UTC)


async def test_the_reconciliation_reads_the_table_back(
    db_engine: AsyncEngine, users: dict[str, UUID]
) -> None:
    """Expected from the plan, actual from the database. The loader returns no
    counts on purpose — a count handed over by the writer is the writer grading
    its own homework."""
    result = await load(db_engine, users, [record()])

    assert result.ok
    assert result.checks[0].expected == result.checks[0].loaded == 1


# --------------------------------------------------------------------------
# Twice — the load that actually tests the trigger
# --------------------------------------------------------------------------


async def test_loading_twice_writes_one_row(db_engine: AsyncEngine, users: dict[str, UUID]) -> None:
    """`ON CONFLICT (legacy_bubble_id)` is the idempotency key, and the ETL's
    recovery plan is a re-run."""
    await load(db_engine, users, [record()])
    await load(db_engine, users, [record()])

    assert len(await stored(db_engine)) == 1


async def test_the_second_load_does_not_stamp_the_row_with_the_import_clock(
    db_engine: AsyncEngine, users: dict[str, UUID]
) -> None:
    """**The failure that only the second load can produce.**

    The upsert takes its `DO UPDATE` branch, `trg_set_updated_at` fires, and
    `updated_at` becomes `now()`. `loader.py` established this by deleting the
    `DISABLE` and watching which tests failed — only the idempotence one did, and
    the timestamp test passed, which is exactly the shape that makes the bug hard
    to find.
    """
    await load(db_engine, users, [record()])
    await load(db_engine, users, [record()])

    row = (await stored(db_engine))[0]
    assert row["updated_at"] == dt.datetime(2025, 9, 3, 9, 31, tzinfo=dt.UTC), (
        "the importer stamped the row; the trigger was not held off"
    )


async def test_an_edited_source_row_is_rewritten_in_place(
    db_engine: AsyncEngine, users: dict[str, UUID]
) -> None:
    """A re-run after Bubble changed something must carry the change, or the
    re-run is a no-op that looks like a success."""
    await load(db_engine, users, [record()])
    await load(db_engine, users, [record(publicReview="Rewritten before cutover.")])

    rows = await stored(db_engine)
    assert len(rows) == 1
    assert rows[0]["public_review"] == "Rewritten before cutover."


# --------------------------------------------------------------------------
# What the loader refuses
# --------------------------------------------------------------------------


async def test_an_unknown_party_raises_rather_than_skipping(
    db_engine: AsyncEngine, users: dict[str, UUID]
) -> None:
    """**A raise, not a skip.** The transform already refuses a review it cannot
    attribute, so reaching this means the transform and the users table disagree
    about who exists — continuing would drop a review with nothing to show for
    it."""
    with pytest.raises(LookupError, match="unknown user"):
        await load(db_engine, users, [record(reviewedBy="somebody-who-never-loaded")])


async def test_a_quarantined_row_is_not_written(
    db_engine: AsyncEngine, users: dict[str, UUID]
) -> None:
    """And the reconciliation still balances: expected counts the plan's rows,
    not the export's."""
    result = await load(
        db_engine, users, [record(), record(**{"unique id": "b", "communicationRating": "2.5"})]
    )

    assert len(await stored(db_engine)) == 1
    assert result.ok


# --------------------------------------------------------------------------
# Reconciliation has to be able to fail
# --------------------------------------------------------------------------


async def test_a_row_that_never_landed_is_named(
    db_engine: AsyncEngine, users: dict[str, UUID]
) -> None:
    """**The negative case, and its absence is why the dead check shipped.**

    Every earlier assertion here was `result.ok`. Replace the body of
    `reconcile_reviews` with one returning empty checks and all of them still
    pass — so the only assertion that could catch a reconciliation which cannot
    fail is one that expects it to.
    """
    plan = plan_reviews([record()], timezone=NEW_YORK)
    async with db_engine.begin() as conn:
        await ReviewLoader(conn).load(users=users, plan=plan)
        await conn.execute(text("DELETE FROM reviews"))
        result = await reconcile_reviews(conn, plan)

    assert not result.ok
    assert result.checks[0].missing == (plan.reviews[0].legacy_bubble_id,)


async def test_a_row_stamped_by_the_importer_is_named(
    db_engine: AsyncEngine, users: dict[str, UUID]
) -> None:
    """The other way a load can be wrong without being short a row.

    Timestamps are compared to the microsecond for exactly this: a load that ran
    with the trigger enabled produces values within a second or two of `now()`,
    so a tolerant comparison passes the failure it exists to catch.
    """
    plan = plan_reviews([record()], timezone=NEW_YORK)
    async with db_engine.begin() as conn:
        await ReviewLoader(conn).load(users=users, plan=plan)
        await conn.execute(text("UPDATE reviews SET updated_at = now()"))
        result = await reconcile_reviews(conn, plan)

    assert not result.ok
    assert result.checks[0].wrong_timestamps == (plan.reviews[0].legacy_bubble_id,)


async def test_a_source_row_the_transform_forgot_is_named(
    db_engine: AsyncEngine, users: dict[str, UUID]
) -> None:
    """**The check that could never fire, now able to.**

    The first version filtered the *plan's own* anchors for falsy ones — every
    anchor is non-empty, so it returned `()` always — and both sides came from
    the plan, which agrees with itself. Reconciliation needs a source list to be
    missing from, which is the defect `reconcile.py` records shipping once
    before.
    """
    plan = plan_reviews([record()], timezone=NEW_YORK)
    forgotten = replace(plan, source_anchors=(*plan.source_anchors, "a-row-nobody-accounted-for"))

    async with db_engine.begin() as conn:
        await ReviewLoader(conn).load(users=users, plan=plan)
        result = await reconcile_reviews(conn, forgotten)

    assert not result.ok
    assert result.unaccounted == ("a-row-nobody-accounted-for",)
    assert "a-row-nobody-accounted-for" in result.report()
