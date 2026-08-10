"""Reconciling an availability load against the plan that produced it.

**The point is the rollback.** `load_profiles.py` reconciles inside the
transaction and raises, because reconciling after a commit reports a problem it
is no longer able to undo. M3 shipped without any of this, so the loader's claim
that Bubble's timestamps survive was asserted by an integration test and by
nothing at run time.

**Row counts do not reconcile one-to-one here, and that is correct.** Every
prior phase compared source rows to loaded rows; availability breaks that three
ways at once — exceptions fan out 1:N, Gen A rules are quarantined on purpose,
and overlapping windows merge so one anchor never reaches the table. Asserting
`source == loaded` would fail every run. What is asserted instead is that every
source row was *accounted for*: loaded, quarantined, dropped, refused, or
absorbed. A row in none of those vanished without anybody deciding it should.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.domain.enums import AvailabilityExceptionType
from app.domain.transform.availability import (
    AvailabilityExceptionRow,
    AvailabilityPlan,
    AvailabilityRuleRow,
)
from app.infra.etl.availability import AvailabilityLoader
from app.infra.etl.reconcile import reconcile_availability

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

MENTOR_BUBBLE_ID = "1720393627919x464416579629646660"
LAGOS = "Africa/Lagos"
BUBBLE_CREATED = dt.datetime(2024, 11, 9, 14, 0, tzinfo=dt.UTC)
BUBBLE_MODIFIED = dt.datetime(2025, 2, 24, 9, 30, tzinfo=dt.UTC)


def rule(anchor: str = "cs-1", **overrides: object) -> AvailabilityRuleRow:
    values: dict[str, object] = {
        "legacy_bubble_id": anchor,
        "mentor_bubble_id": MENTOR_BUBBLE_ID,
        "day_of_week": 1,
        "start_time": dt.time(9, 0),
        "end_time": dt.time(12, 0),
        "timezone": LAGOS,
        "is_active": True,
        "created_at": BUBBLE_CREATED,
        "updated_at": BUBBLE_MODIFIED,
    }
    values.update(overrides)
    return AvailabilityRuleRow(**values)  # type: ignore[arg-type]


def exception_row(anchor: str = "ce-1:2025-01-13", day: int = 13) -> AvailabilityExceptionRow:
    return AvailabilityExceptionRow(
        legacy_bubble_id=anchor,
        mentor_bubble_id=MENTOR_BUBBLE_ID,
        kind=AvailabilityExceptionType.BLOCK,
        start_date=dt.date(2025, 1, day),
        end_date=dt.date(2025, 1, day + 1),
        timezone=LAGOS,
        max_sessions_per_day=None,
        created_at=BUBBLE_CREATED,
        updated_at=BUBBLE_MODIFIED,
    )


@pytest_asyncio.fixture
async def mentor(db_engine: AsyncEngine) -> AsyncIterator[tuple[AsyncConnection, dict[str, UUID]]]:
    async with db_engine.begin() as conn:
        user_id = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, primary_role, timezone) "
                    "VALUES ('rec@example.test', 'mentor', :tz) RETURNING id"
                ),
                {"tz": LAGOS},
            )
        ).scalar_one()
        await conn.execute(
            text("INSERT INTO mentor_profiles (user_id, headline) VALUES (:u, 'M')"),
            {"u": user_id},
        )
        yield conn, {MENTOR_BUBBLE_ID: user_id}


async def load_and_reconcile(
    conn: AsyncConnection, users: dict[str, UUID], plan: AvailabilityPlan
) -> object:
    await AvailabilityLoader(conn).load(users=users, rules=plan.rules, exceptions=plan.exceptions)
    return await reconcile_availability(conn, plan)


async def test_a_clean_load_reconciles(
    mentor: tuple[AsyncConnection, dict[str, UUID]],
) -> None:
    conn, users = mentor
    plan = AvailabilityPlan(rules=(rule(),), exceptions=(exception_row(),))

    result = await load_and_reconcile(conn, users, plan)

    assert result.ok, result.report()  # type: ignore[attr-defined]


async def test_a_row_the_plan_expected_but_the_table_lacks_is_a_failure(
    mentor: tuple[AsyncConnection, dict[str, UUID]],
) -> None:
    """The case reconciliation exists for: the loader reported success and one
    row is not there."""
    conn, users = mentor
    plan = AvailabilityPlan(rules=(rule("cs-1"), rule("cs-2", day_of_week=2)), exceptions=())
    await AvailabilityLoader(conn).load(users=users, rules=plan.rules, exceptions=())
    await conn.execute(text("DELETE FROM availability_rules WHERE legacy_bubble_id = 'cs-2'"))

    result = await reconcile_availability(conn, plan)

    assert not result.ok
    assert "cs-2" in result.report()


async def test_a_row_nobody_planned_is_a_failure(
    mentor: tuple[AsyncConnection, dict[str, UUID]],
) -> None:
    """Counting only what is missing would pass a table with extra rows in it —
    a re-run against a changed transform leaves exactly that."""
    conn, users = mentor
    plan = AvailabilityPlan(rules=(rule("cs-1"),), exceptions=())
    await AvailabilityLoader(conn).load(
        users=users, rules=(rule("cs-1"), rule("cs-9", day_of_week=3)), exceptions=()
    )

    result = await reconcile_availability(conn, plan)

    assert not result.ok


async def test_a_timestamp_rewritten_by_the_importer_is_a_failure(
    mentor: tuple[AsyncConnection, dict[str, UUID]],
) -> None:
    """The loader's central claim, checked against this run rather than trusted.

    Comparison is to the microsecond, for the reason `reconcile_users` gives: a
    load that ran with the trigger enabled produces values within a second or
    two of each other and of `now()`, so a tolerant comparison passes exactly
    the failure this exists to catch.
    """
    conn, users = mentor
    plan = AvailabilityPlan(rules=(rule(),), exceptions=())
    await AvailabilityLoader(conn).load(users=users, rules=plan.rules, exceptions=())
    await conn.execute(text("UPDATE availability_rules SET updated_at = now()"))

    result = await reconcile_availability(conn, plan)

    assert not result.ok
    assert "cs-1" in result.report()


async def test_quarantined_rules_are_absent_and_that_is_not_a_failure(
    mentor: tuple[AsyncConnection, dict[str, UUID]],
) -> None:
    """Gen A rules are deliberately not loaded. A reconciliation that treated
    absence as a defect would fail every run against real data, and the fix
    somebody would reach for is a loosened comparison."""
    from app.domain.transform.availability import QuarantinedRule

    conn, users = mentor
    plan = AvailabilityPlan(
        rules=(rule(),),
        exceptions=(),
        quarantined=(
            QuarantinedRule(
                legacy_bubble_id="cs-genA",
                mentor_bubble_id=MENTOR_BUBBLE_ID,
                day_of_week=1,
                as_printed=dt.time(2, 0),
                as_displayed=dt.time(7, 0),
                end_as_printed=dt.time(2, 15),
                end_as_displayed=dt.time(7, 15),
            ),
        ),
    )

    result = await load_and_reconcile(conn, users, plan)

    assert result.ok, result.report()  # type: ignore[attr-defined]


async def test_a_source_row_the_transform_decided_nothing_about_is_a_failure(
    mentor: tuple[AsyncConnection, dict[str, UUID]],
) -> None:
    """Silent data loss, and the one failure no row count can show.

    The loaded total is *meant* to be smaller than the source total here —
    quarantine and merging both shrink it — so a row that simply disappeared
    looks exactly like a row that was correctly withheld. Only the accounting
    identity separates them.

    Written after noticing `_unaccounted` returned duplicates while its docstring
    claimed to do this, and that every test passed regardless: none of them gave
    the plan a source list to be missing from.
    """
    conn, users = mentor
    plan = AvailabilityPlan(
        rules=(rule("cs-1"),),
        exceptions=(),
        # Two source rows in, one accounted for. The second was neither loaded
        # nor recorded as dropped — it vanished.
        source_rule_ids=("cs-1", "cs-vanished"),
    )

    result = await load_and_reconcile(conn, users, plan)

    assert not result.ok  # type: ignore[attr-defined]
    assert "cs-vanished" in result.report()  # type: ignore[attr-defined]


async def test_a_fan_out_date_that_fails_to_land_is_caught(
    mentor: tuple[AsyncConnection, dict[str, UUID]],
) -> None:
    """One legacy row produces several dated rows, and losing one must fail.

    There is deliberately **no** separate per-source-row fan-out check. One was
    written and removed after a mutation batch showed it survived every test: a
    date that fails to land is already absent from `actual` and is caught here,
    and one that lands unplanned makes `expected` and `loaded` disagree. This
    test is what covers the case; the extra check only re-stated it.
    """
    conn, users = mentor
    plan = AvailabilityPlan(
        rules=(),
        exceptions=(
            exception_row("ce-1:2025-01-13", 13),
            exception_row("ce-1:2025-01-19", 19),
            exception_row("ce-2:2025-01-21", 21),
        ),
    )
    await AvailabilityLoader(conn).load(users=users, rules=(), exceptions=plan.exceptions)
    await conn.execute(
        text("DELETE FROM availability_exceptions WHERE legacy_bubble_id = 'ce-1:2025-01-19'")
    )

    result = await reconcile_availability(conn, plan)

    assert not result.ok
    assert "ce-1:2025-01-19" in result.report()
