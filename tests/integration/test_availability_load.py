"""Loading availability, twice, with Bubble's timestamps intact.

**The second run is the test.** ``trg_set_updated_at`` is a ``BEFORE UPDATE``
trigger, so a first load into empty tables preserves Bubble's timestamps whether
or not anything disables it — a suite that loads once and checks the values
passes against a loader with no hold-off at all. It is the re-run, taking the
``DO UPDATE`` branch, that rewrites every migrated ``updated_at`` to the import
clock. The ETL is required to be re-runnable, so that is the normal path.

The other thing only a database can show is the exclusion constraint. The
transform merges overlapping windows before they reach the loader; if it ever
stopped, the failure is not a strange row but an aborted transaction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.domain.enums import AvailabilityExceptionType
from app.domain.transform.availability import AvailabilityExceptionRow, AvailabilityRuleRow
from app.infra.etl.availability import AvailabilityLoader

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

MENTOR_BUBBLE_ID = "1720393627919x464416579629646660"
LAGOS = "Africa/Lagos"

#: Deliberately in the past. `now()` would be indistinguishable from the import
#: clock, which is the thing under test.
BUBBLE_CREATED = datetime(2024, 11, 9, 14, 0, tzinfo=UTC)
BUBBLE_MODIFIED = datetime(2025, 2, 24, 9, 30, tzinfo=UTC)


def rule(**overrides: object) -> AvailabilityRuleRow:
    values: dict[str, object] = {
        "legacy_bubble_id": "cs-1",
        "mentor_bubble_id": MENTOR_BUBBLE_ID,
        "day_of_week": 1,
        "start_time": time(9, 0),
        "end_time": time(12, 0),
        "timezone": LAGOS,
        "is_active": True,
        "created_at": BUBBLE_CREATED,
        "updated_at": BUBBLE_MODIFIED,
    }
    values.update(overrides)
    return AvailabilityRuleRow(**values)  # type: ignore[arg-type]


def exception_row(**overrides: object) -> AvailabilityExceptionRow:
    values: dict[str, object] = {
        "legacy_bubble_id": "ce-1:2025-01-13",
        "mentor_bubble_id": MENTOR_BUBBLE_ID,
        "kind": AvailabilityExceptionType.BLOCK,
        "start_date": date(2025, 1, 13),
        "end_date": date(2025, 1, 14),
        "timezone": LAGOS,
        "max_sessions_per_day": None,
        "created_at": BUBBLE_CREATED,
        "updated_at": BUBBLE_MODIFIED,
    }
    values.update(overrides)
    return AvailabilityExceptionRow(**values)  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def mentor(db_engine: AsyncEngine) -> AsyncIterator[tuple[AsyncConnection, dict[str, UUID]]]:
    """One approved mentor, and the bubble-id to user-id map the loader takes."""
    async with db_engine.begin() as conn:
        user_id = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, primary_role, timezone) "
                    "VALUES ('mentor@example.test', 'mentor', :tz) RETURNING id"
                ),
                {"tz": LAGOS},
            )
        ).scalar_one()
        await conn.execute(
            text("INSERT INTO mentor_profiles (user_id, headline) VALUES (:u, 'Test mentor')"),
            {"u": user_id},
        )
        yield conn, {MENTOR_BUBBLE_ID: user_id}


async def test_rules_and_exceptions_load(
    mentor: tuple[AsyncConnection, dict[str, UUID]],
) -> None:
    conn, users = mentor

    await AvailabilityLoader(conn).load(users=users, rules=[rule()], exceptions=[exception_row()])

    # Asserted against the database, not against a count the loader returned.
    # It used to return `len(rules)` — the number of rows handed *in*, which is
    # an assertion about the table made without asking it. `reconcile_availability`
    # is what reads back what actually landed.
    totals = await conn.execute(
        text(
            "SELECT (SELECT count(*) FROM availability_rules), "
            "(SELECT count(*) FROM availability_exceptions)"
        )
    )
    assert totals.one() == (1, 1)
    stored = await conn.execute(
        text("SELECT start_time, end_time, timezone, is_active FROM availability_rules")
    )
    assert stored.one() == (time(9, 0), time(12, 0), LAGOS, True)


async def test_the_exception_range_is_half_open(
    mentor: tuple[AsyncConnection, dict[str, UUID]],
) -> None:
    """A single blocked day is ``[d, d+1)``. Inclusive-upper would make "one day"
    ambiguous and answer overlap queries differently."""
    conn, users = mentor
    await AvailabilityLoader(conn).load(users=users, rules=[], exceptions=[exception_row()])

    covered = await conn.execute(
        text(
            "SELECT date_range @> DATE '2025-01-13', date_range @> DATE '2025-01-14' "
            "FROM availability_exceptions"
        )
    )
    assert covered.one() == (True, False)


async def test_a_second_run_changes_nothing(
    mentor: tuple[AsyncConnection, dict[str, UUID]],
) -> None:
    """Idempotent on ``legacy_bubble_id`` — the recovery plan is a re-run."""
    conn, users = mentor
    loader = AvailabilityLoader(conn)

    await loader.load(users=users, rules=[rule()], exceptions=[exception_row()])
    await loader.load(users=users, rules=[rule()], exceptions=[exception_row()])

    totals = await conn.execute(
        text(
            "SELECT (SELECT count(*) FROM availability_rules), "
            "(SELECT count(*) FROM availability_exceptions)"
        )
    )
    assert totals.one() == (1, 1)


async def test_bubble_timestamps_survive_a_re_run(
    mentor: tuple[AsyncConnection, dict[str, UUID]],
) -> None:
    """The assertion the whole hold-off exists for.

    Deleting ``timestamps_from_source_across`` from the loader leaves
    ``test_a_second_run_changes_nothing`` green — the counts are right either
    way. Only this notices, and only because it runs twice.
    """
    conn, users = mentor
    loader = AvailabilityLoader(conn)

    await loader.load(users=users, rules=[rule()], exceptions=[exception_row()])
    await loader.load(users=users, rules=[rule()], exceptions=[exception_row()])

    stamps = await conn.execute(text("SELECT created_at, updated_at FROM availability_rules"))
    assert stamps.one() == (BUBBLE_CREATED, BUBBLE_MODIFIED)


async def test_a_changed_source_row_is_updated_in_place(
    mentor: tuple[AsyncConnection, dict[str, UUID]],
) -> None:
    """Idempotence must not mean inertness: a re-run after the source changed has
    to move the row, or a corrected transform could never be re-applied."""
    conn, users = mentor
    loader = AvailabilityLoader(conn)

    await loader.load(users=users, rules=[rule()], exceptions=[])
    await loader.load(users=users, rules=[rule(end_time=time(13, 0))], exceptions=[])

    stored = await conn.execute(text("SELECT end_time FROM availability_rules"))
    assert stored.scalar_one() == time(13, 0)


async def test_an_unmerged_overlap_aborts_the_load(
    mentor: tuple[AsyncConnection, dict[str, UUID]],
) -> None:
    """What the transform's merge is protecting against, stated as behaviour.

    The exclusion constraint means an overlapping pair does not land as an odd
    row — it raises and takes the transaction with it. This is why merging
    happens in the transform rather than being left to the database to sort out.
    """
    conn, users = mentor

    with pytest.raises(Exception, match="availability_rules_no_overlap"):
        await AvailabilityLoader(conn).load(
            users=users,
            rules=[
                rule(legacy_bubble_id="a", start_time=time(9, 0), end_time=time(12, 0)),
                rule(legacy_bubble_id="b", start_time=time(10, 0), end_time=time(13, 0)),
            ],
            exceptions=[],
        )


async def test_an_unknown_mentor_refuses_rather_than_skips(
    mentor: tuple[AsyncConnection, dict[str, UUID]],
) -> None:
    """Skipping silently would drop a mentor's availability with nothing to show
    for it. The transform already refuses these; reaching here means the two
    disagree about who exists, which is worth a crash."""
    conn, users = mentor

    with pytest.raises(LookupError, match="unknown user"):
        await AvailabilityLoader(conn).load(
            users=users, rules=[rule(mentor_bubble_id="nobody")], exceptions=[]
        )
