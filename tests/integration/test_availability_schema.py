"""What the M3 availability tables guarantee, and what no gate can see.

`alembic check` reads tables, columns, types and regular indexes. Everything this
file asserts is outside that set — two CHECK constraints per table, a partial
index, a GiST index over an operator class, and the cascade behaviour of two
foreign keys. A green migration check says the chain applied, not that any of
this holds.

The `updated_at` trigger is deliberately **not** re-asserted here:
``test_reference_data`` already sweeps every table carrying the column and fails
when one has no trigger, so a copy would be a second representation of one rule
(non-negotiable #8). The same goes for enum labels, surrogate primary keys and
CHECK-constraint naming, all swept by ``test_schema_parity``.

Every constraint gets a rejecting **and** an accepting case. A test that only
proves a constraint refuses garbage cannot tell a working constraint from one
that refuses everything.
"""

from collections.abc import AsyncIterator
from datetime import date, time

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

LAGOS = "Africa/Lagos"

INSERT_RULE = """
INSERT INTO availability_rules (mentor_user_id, day_of_week, start_time, end_time, timezone)
VALUES (:mentor, :dow, :start, :end, :tz)
"""

INSERT_EXCEPTION = """
INSERT INTO availability_exceptions
    (mentor_user_id, type, date_range, start_time, end_time, timezone)
VALUES (:mentor, :kind, daterange(:lo, :hi, '[)'), :start, :end, :tz)
"""


def clock(value: str | None) -> time | None:
    """``"09:00"`` as a real ``time``.

    asyncpg binds `time` parameters through the binary protocol and refuses a
    string outright, so the conversion has to happen here rather than in the
    database. Written once: a `::time` cast repeated at each call site is the
    second representation of one rule that decision #43 is about.
    """
    return time.fromisoformat(value) if value is not None else None


@pytest_asyncio.fixture
async def mentor(db_engine: AsyncEngine) -> AsyncIterator[tuple[AsyncConnection, str]]:
    """One approved mentor, and the connection its rows live on.

    `availability_rules.mentor_user_id` references `mentor_profiles(user_id)`
    rather than `users(id)` — the same choice `mentor_status_events` records.
    Both hold the same value; only one of them guarantees a profile exists, and
    a rule belonging to a user who is not a mentor is not a thing this schema
    should be able to represent.
    """
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
        yield conn, user_id


async def insert_rule(
    conn: AsyncConnection, mentor_id: str, *, dow: int = 1, start: str = "09:00", end: str = "12:00"
) -> None:
    await conn.execute(
        text(INSERT_RULE),
        {
            "mentor": mentor_id,
            "dow": dow,
            "start": clock(start),
            "end": clock(end),
            "tz": LAGOS,
        },
    )


async def insert_exception(
    conn: AsyncConnection,
    mentor_id: str,
    *,
    kind: str = "block",
    lo: date = date(2026, 3, 1),
    hi: date = date(2026, 3, 8),
    start: str | None = None,
    end: str | None = None,
) -> None:
    await conn.execute(
        text(INSERT_EXCEPTION),
        {
            "mentor": mentor_id,
            "kind": kind,
            "lo": lo,
            "hi": hi,
            "start": clock(start),
            "end": clock(end),
            "tz": LAGOS,
        },
    )


# --------------------------------------------------------------------------
# availability_rules
# --------------------------------------------------------------------------


async def test_a_well_formed_rule_is_accepted(
    mentor: tuple[AsyncConnection, str],
) -> None:
    """The positive case, first.

    Every rejection below is only meaningful against this — a table that refused
    all four would pass every negative test in this file.
    """
    conn, mentor_id = mentor

    await insert_rule(conn, mentor_id, dow=1, start="09:00", end="12:00")

    count = await conn.execute(text("SELECT count(*) FROM availability_rules"))
    assert count.scalar_one() == 1


@pytest.mark.parametrize("day", [0, 6])
async def test_both_ends_of_the_weekday_range_are_legal(
    mentor: tuple[AsyncConnection, str], day: int
) -> None:
    """0 is Sunday and 6 is Saturday, per the package and the legacy data.

    Verified against the export before it was relied on: `dayOfWeekIn` and
    `daysOfWeek-O/S` agree on all 24 dev rows, and 0 is Sunday in every one.
    An off-by-one here moves every migrated mentor's availability by a day.
    """
    conn, mentor_id = mentor

    await insert_rule(conn, mentor_id, dow=day)

    stored = await conn.execute(text("SELECT day_of_week FROM availability_rules"))
    assert stored.scalar_one() == day


@pytest.mark.parametrize("day", [-1, 7])
async def test_a_weekday_outside_the_range_is_refused(
    mentor: tuple[AsyncConnection, str], day: int
) -> None:
    """7 is the interesting one: a caller counting Monday-as-1 through Sunday-as-7."""
    conn, mentor_id = mentor

    with pytest.raises(IntegrityError, match="availability_rules_day_of_week"):
        await insert_rule(conn, mentor_id, dow=day)


@pytest.mark.parametrize(("start", "end"), [("12:00", "09:00"), ("09:00", "09:00")])
async def test_a_window_that_does_not_move_forward_is_refused(
    mentor: tuple[AsyncConnection, str], start: str, end: str
) -> None:
    """Inverted and zero-length, because the constraint is `>` and not `>=`.

    A zero-length window is the one a reader assumes is caught by "ordered" and
    is not, unless the operator is strict.
    """
    conn, mentor_id = mentor

    with pytest.raises(IntegrityError, match="availability_window_ordered"):
        await insert_rule(conn, mentor_id, start=start, end=end)


async def test_a_mentor_may_hold_several_windows_on_one_day(
    mentor: tuple[AsyncConnection, str],
) -> None:
    """Split availability — morning and afternoon with a lunch gap.

    The legacy one-row-per-day shape could not express this, and the dev export
    contains 6 days that carry more than one row, up to 3 on one day. So this is
    a property the migration depends on rather than a capability held in reserve:
    a unique constraint on `(mentor_user_id, day_of_week)` would reject real
    rows, which is exactly why there isn't one.
    """
    conn, mentor_id = mentor

    await insert_rule(conn, mentor_id, dow=1, start="09:00", end="12:00")
    await insert_rule(conn, mentor_id, dow=1, start="14:00", end="17:00")

    count = await conn.execute(
        text("SELECT count(*) FROM availability_rules WHERE day_of_week = 1")
    )
    assert count.scalar_one() == 2


# --------------------------------------------------------------------------
# availability_exceptions
# --------------------------------------------------------------------------


async def test_a_whole_day_block_carries_no_times(
    mentor: tuple[AsyncConnection, str],
) -> None:
    """Both times null is the whole-day case, and it is what the ETL writes.

    Legacy `CalendarExtra` holds only dates, so every migrated exception lands
    here. If this were refused the entire M3 exception load would fail.
    """
    conn, mentor_id = mentor

    await insert_exception(conn, mentor_id, start=None, end=None)

    stored = await conn.execute(text("SELECT start_time, end_time FROM availability_exceptions"))
    assert stored.one() == (None, None)


async def test_a_partial_day_block_carries_both_times(
    mentor: tuple[AsyncConnection, str],
) -> None:
    conn, mentor_id = mentor

    await insert_exception(conn, mentor_id, start="09:00", end="12:00")

    count = await conn.execute(text("SELECT count(*) FROM availability_exceptions"))
    assert count.scalar_one() == 1


@pytest.mark.parametrize(("start", "end"), [("09:00", None), (None, "12:00")])
async def test_half_a_time_pair_is_refused(
    mentor: tuple[AsyncConnection, str], start: str | None, end: str | None
) -> None:
    """A start with no end has no defensible reading — open-ended, or midnight?

    Refusing it is what keeps the projection from having to guess.
    """
    conn, mentor_id = mentor

    with pytest.raises(IntegrityError, match="exception_times_paired"):
        await insert_exception(conn, mentor_id, start=start, end=end)


async def test_an_inverted_exception_window_is_refused(
    mentor: tuple[AsyncConnection, str],
) -> None:
    conn, mentor_id = mentor

    with pytest.raises(IntegrityError, match="exception_window_ordered"):
        await insert_exception(conn, mentor_id, start="12:00", end="09:00")


async def test_overlapping_ranges_are_found_by_the_range_operator(
    mentor: tuple[AsyncConnection, str],
) -> None:
    """The read the GiST index exists to serve.

    Asserted as behaviour rather than as an index definition: a query returning
    the wrong rows is the failure, and an index is only how it stays fast.
    """
    conn, mentor_id = mentor

    await insert_exception(conn, mentor_id, lo=date(2026, 3, 1), hi=date(2026, 3, 8))
    await insert_exception(conn, mentor_id, lo=date(2026, 6, 1), hi=date(2026, 6, 3))

    hits = await conn.execute(
        text(
            "SELECT count(*) FROM availability_exceptions "
            "WHERE mentor_user_id = :m AND date_range && daterange(:lo, :hi, '[)')"
        ),
        {"m": mentor_id, "lo": date(2026, 3, 5), "hi": date(2026, 3, 6)},
    )
    assert hits.scalar_one() == 1


# --------------------------------------------------------------------------
# The objects `alembic check` cannot see
# --------------------------------------------------------------------------


async def test_the_exception_range_index_is_gist_over_both_columns(
    db_engine: AsyncEngine,
) -> None:
    """A btree here would apply and then answer overlap queries badly.

    Both halves are asserted. `USING gist` alone would pass with the mentor
    column dropped, and the two-column form is what needs `btree_gist` — uuid
    has no GiST operator class without it, so this test is also what proves the
    extension is present rather than assumed.
    """
    async with db_engine.connect() as conn:
        definition = await conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND indexname = :name"
            ),
            {"name": "ix_availability_exceptions_range"},
        )
        sql = definition.scalar_one()

    assert "USING gist" in sql
    assert "mentor_user_id" in sql
    assert "date_range" in sql


async def test_the_rule_lookup_index_excludes_inactive_and_deleted_rows(
    db_engine: AsyncEngine,
) -> None:
    """The WHERE clause is the whole point, and it is invisible to the gate.

    Without it the index still applies and still speeds the query up, so nothing
    fails — it simply carries rows the read never wants. `alembic check` cannot
    compare a partial index's predicate at all.
    """
    async with db_engine.connect() as conn:
        definition = await conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND indexname = :name"
            ),
            {"name": "ix_availability_rules_mentor"},
        )
        sql = definition.scalar_one()

    assert "WHERE" in sql
    assert "is_active" in sql
    assert "deleted_at IS NULL" in sql


async def test_deleting_a_mentor_takes_their_availability_with_it(
    mentor: tuple[AsyncConnection, str],
) -> None:
    """ADR 0013 applied: a rule is meaningless without its mentor and records no
    auditable fact, so it cascades.

    The positive case for the cascade. ``test_schema_parity`` asserts the
    *negative* property — that no cascade path reaches a table which must be
    retained — and the two together are what make the edge deliberate rather
    than inherited from the package's DDL.
    """
    conn, mentor_id = mentor
    await insert_rule(conn, mentor_id)
    await insert_exception(conn, mentor_id)

    await conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": mentor_id})

    rules = await conn.execute(text("SELECT count(*) FROM availability_rules"))
    exceptions = await conn.execute(text("SELECT count(*) FROM availability_exceptions"))
    assert (rules.scalar_one(), exceptions.scalar_one()) == (0, 0)
