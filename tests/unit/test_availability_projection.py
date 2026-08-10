"""Projecting recurring availability onto real instants, across DST.

Every expected value here was **measured against the tz database before it was
written down**, not derived by hand. The two transition dates are the ones the
milestone exists for:

- ``2026-03-08`` — ``America/New_York`` springs forward, 02:00 becomes 03:00, so
  the local hour ``02:00-02:59`` does not exist.
- ``2026-11-01`` — it falls back, 02:00 becomes 01:00, so ``01:00-01:59``
  happens twice.

Both are Sundays, because US transitions always are — which is why several tests
use ``day_of_week=0``.

``Africa/Lagos`` appears throughout as the control. It has never observed DST, so
a test that passes only there proves the arithmetic and nothing about the hard
part; a test that passes only in New York might be passing by accident on a date
where the two readings agree. Both are needed.

**RFC 5545 §3.3.5 is the specification**, and Python's default ``fold=0``
implements it exactly:

- a local time that occurs twice resolves to the **first** occurrence
- a local time that does not occur is interpreted with the offset **before** the
  gap

That equivalence is measured, not assumed, and ``test_fold_zero_is_rfc_5545`` is
the test that fails if a future reader "fixes" the default.
"""

from datetime import UTC, date, datetime, time

import pytest

from app.domain.availability import (
    DatedException,
    UtcInterval,
    WeeklyWindow,
    project,
)
from app.domain.enums import AvailabilityExceptionType

NEW_YORK = "America/New_York"
LAGOS = "Africa/Lagos"

SPRING_FORWARD = date(2026, 3, 8)
FALL_BACK = date(2026, 11, 1)

SUNDAY = 0
MONDAY = 1


def window(
    *, day: int = MONDAY, start: str = "09:00", end: str = "12:00", tz: str = LAGOS
) -> WeeklyWindow:
    return WeeklyWindow(
        day_of_week=day,
        start_time=time.fromisoformat(start),
        end_time=time.fromisoformat(end),
        timezone=tz,
    )


def block(
    *,
    start_date: date,
    end_date: date | None = None,
    start: str | None = None,
    end: str | None = None,
    tz: str = LAGOS,
    kind: AvailabilityExceptionType = AvailabilityExceptionType.BLOCK,
) -> DatedException:
    return DatedException(
        kind=kind,
        start_date=start_date,
        # Exclusive upper bound, matching the `daterange [)` in the schema.
        end_date=end_date or date.fromordinal(start_date.toordinal() + 1),
        start_time=time.fromisoformat(start) if start else None,
        end_time=time.fromisoformat(end) if end else None,
        timezone=tz,
    )


def utc(moment: str) -> datetime:
    return datetime.fromisoformat(moment).replace(tzinfo=UTC)


def one_day(day: date) -> dict[str, date]:
    return {"start": day, "end": date.fromordinal(day.toordinal() + 1)}


# --------------------------------------------------------------------------
# The base case, in a zone that never shifts
# --------------------------------------------------------------------------


def test_a_window_in_a_zone_without_dst_projects_to_the_obvious_instants() -> None:
    """Lagos is UTC+1 always. If this fails, the arithmetic is wrong before DST
    has been reached at all."""
    result = project(
        rules=[window(day=MONDAY, start="09:00", end="12:00", tz=LAGOS)],
        exceptions=[],
        **one_day(date(2026, 3, 9)),  # a Monday
    )

    assert result == (UtcInterval(utc("2026-03-09T08:00:00"), utc("2026-03-09T11:00:00")),)


def test_a_weekday_the_rule_does_not_name_yields_nothing() -> None:
    result = project(
        rules=[window(day=MONDAY)],
        exceptions=[],
        **one_day(date(2026, 3, 10)),  # a Tuesday
    )

    assert result == ()


def test_day_of_week_zero_is_sunday() -> None:
    """The legacy `dayOfWeekIn` convention, verified against the export: 0 is
    Sunday on all 24 dev rows. An off-by-one here moves every migrated mentor's
    availability by a day and nothing downstream would notice."""
    result = project(
        rules=[window(day=SUNDAY, start="09:00", end="12:00", tz=LAGOS)],
        exceptions=[],
        **one_day(date(2026, 3, 15)),  # a Sunday
    )

    assert len(result) == 1


# --------------------------------------------------------------------------
# DST — the reason this module exists
# --------------------------------------------------------------------------


def test_a_window_spanning_spring_forward_loses_an_hour_of_real_time() -> None:
    """09:00-12:00 stays three hours; 01:00-05:00 does not stay four.

    The clock says four hours. Only three of them exist. That is correct: the
    mentor's day genuinely is an hour shorter, and preserving the duration
    instead would silently move the window's end past what they declared.
    """
    result = project(
        rules=[window(day=SUNDAY, start="01:00", end="05:00", tz=NEW_YORK)],
        exceptions=[],
        **one_day(SPRING_FORWARD),
    )

    assert result == (UtcInterval(utc("2026-03-08T06:00:00"), utc("2026-03-08T09:00:00")),)
    assert result[0].end - result[0].start == (
        utc("2026-01-01T03:00:00") - utc("2026-01-01T00:00:00")
    )


def test_a_window_entirely_inside_the_spring_forward_gap_is_dropped() -> None:
    """02:00-03:00 on this date resolves to 07:00Z-07:00Z — zero length.

    The database `CHECK (end_time > start_time)` runs on wall clock and cannot
    see this; both endpoints are perfectly ordered as declared. A zero-length
    interval reaching a caller would render as a bookable window of no duration.
    """
    result = project(
        rules=[window(day=SUNDAY, start="02:00", end="03:00", tz=NEW_YORK)],
        exceptions=[],
        **one_day(SPRING_FORWARD),
    )

    assert result == ()


def test_a_window_spanning_fall_back_gains_an_hour_of_real_time() -> None:
    """00:00-03:00 is three hours on the clock and four in reality."""
    result = project(
        rules=[window(day=SUNDAY, start="00:00", end="03:00", tz=NEW_YORK)],
        exceptions=[],
        **one_day(FALL_BACK),
    )

    assert result == (UtcInterval(utc("2026-11-01T04:00:00"), utc("2026-11-01T08:00:00")),)


def test_an_ambiguous_window_takes_the_first_occurrence() -> None:
    """Each **endpoint** resolves to its first occurrence. RFC 5545 §3.3.5.

    Note what this does and does not say. The start resolves to 05:00Z, the
    first 01:00; the end resolves to 07:00Z, which is 02:00 EST. The resulting
    span is two real hours and therefore *does* contain both occurrences of the
    repeated hour — because the endpoints resolve independently and duration is
    not preserved. The rule governs each endpoint, not the span, and reading it
    as "only one of the two 01:00s is offered" is the misreading to avoid.
    """
    result = project(
        rules=[window(day=SUNDAY, start="01:00", end="02:00", tz=NEW_YORK)],
        exceptions=[],
        **one_day(FALL_BACK),
    )

    assert result == (UtcInterval(utc("2026-11-01T05:00:00"), utc("2026-11-01T07:00:00")),)


def test_fold_zero_is_rfc_5545() -> None:
    """Pins the equivalence the whole module rests on.

    ``fold=0`` is Python's default *and* is now normalised explicitly in
    ``_resolve``, because ``datetime.combine`` inherits ``fold`` from the
    ``time`` it is given — so the default alone did not guarantee it. A future
    reader "correcting" either to ``fold=1`` would silently invert both DST
    branches, and every other test here would still describe behaviour that
    looked entirely plausible.
    """
    ambiguous = project(
        rules=[window(day=SUNDAY, start="01:30", end="01:45", tz=NEW_YORK)],
        exceptions=[],
        **one_day(FALL_BACK),
    )
    nonexistent = project(
        rules=[window(day=SUNDAY, start="02:30", end="02:45", tz=NEW_YORK)],
        exceptions=[],
        **one_day(SPRING_FORWARD),
    )

    # First occurrence: EDT (-04:00), not EST (-05:00) an hour later.
    assert ambiguous[0].start == utc("2026-11-01T05:30:00")
    # Pre-gap offset: EST (-05:00), which lands after the gap in real time.
    assert nonexistent[0].start == utc("2026-03-08T07:30:00")


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------


def test_a_whole_day_block_removes_the_day() -> None:
    result = project(
        rules=[window(day=MONDAY, tz=LAGOS)],
        exceptions=[block(start_date=date(2026, 3, 9), tz=LAGOS)],
        **one_day(date(2026, 3, 9)),
    )

    assert result == ()


def test_a_partial_day_block_splits_one_window_in_two() -> None:
    """09:00-12:00 minus 10:00-11:00 is two windows, not one shortened one."""
    result = project(
        rules=[window(day=MONDAY, start="09:00", end="12:00", tz=LAGOS)],
        exceptions=[block(start_date=date(2026, 3, 9), start="10:00", end="11:00", tz=LAGOS)],
        **one_day(date(2026, 3, 9)),
    )

    assert result == (
        UtcInterval(utc("2026-03-09T08:00:00"), utc("2026-03-09T09:00:00")),
        UtcInterval(utc("2026-03-09T10:00:00"), utc("2026-03-09T11:00:00")),
    )


def test_a_block_starting_exactly_where_the_window_does_leaves_no_empty_span() -> None:
    """A mentor blocking the first hour of their morning — an ordinary case.

    Found by a mutation, not by design: relaxing the mid-block guard in
    `_subtract` from `>` to `>=` survived the whole suite, because every block
    in it started strictly inside its window. With the block's start equal to
    the window's, the relaxed version emits a zero-length interval before the
    real one.
    """
    result = project(
        rules=[window(day=MONDAY, start="09:00", end="12:00", tz=LAGOS)],
        exceptions=[block(start_date=date(2026, 3, 9), start="09:00", end="10:00", tz=LAGOS)],
        **one_day(date(2026, 3, 9)),
    )

    assert result == (UtcInterval(utc("2026-03-09T09:00:00"), utc("2026-03-09T11:00:00")),)


def test_a_block_ending_exactly_where_the_window_does_leaves_no_empty_span() -> None:
    """The mirror case — blocking the last hour."""
    result = project(
        rules=[window(day=MONDAY, start="09:00", end="12:00", tz=LAGOS)],
        exceptions=[block(start_date=date(2026, 3, 9), start="11:00", end="12:00", tz=LAGOS)],
        **one_day(date(2026, 3, 9)),
    )

    assert result == (UtcInterval(utc("2026-03-09T08:00:00"), utc("2026-03-09T10:00:00")),)


def test_a_block_resolves_in_its_own_timezone_not_the_rules() -> None:
    """The edge case most likely to be wrong and least likely to be noticed.

    Both tables carry their own `timezone`, and they can disagree — a mentor who
    moved, or an exception entered from a different device. Resolving the block
    in the rule's zone would subtract the right *clock* hours from the wrong
    *real* hours, and the result would look entirely reasonable.

    Here the rule is 09:00-12:00 Lagos (08:00-11:00Z) and the block is
    10:00-11:00 **New York**, which is 15:00-16:00Z — outside the window
    entirely. Nothing may be subtracted. Read in the rule's zone it would have
    removed the middle hour.
    """
    result = project(
        rules=[window(day=MONDAY, start="09:00", end="12:00", tz=LAGOS)],
        exceptions=[block(start_date=date(2026, 3, 9), start="10:00", end="11:00", tz=NEW_YORK)],
        **one_day(date(2026, 3, 9)),
    )

    assert result == (UtcInterval(utc("2026-03-09T08:00:00"), utc("2026-03-09T11:00:00")),)


@pytest.mark.parametrize(
    ("day", "expected_start", "expected_end"),
    [
        (SPRING_FORWARD, "2026-03-08T05:00:00", "2026-03-09T04:00:00"),  # 23 hours
        (FALL_BACK, "2026-11-01T04:00:00", "2026-11-02T05:00:00"),  # 25 hours
    ],
    ids=["spring-forward-23h", "fall-back-25h"],
)
def test_a_whole_day_in_a_shifting_zone_is_not_24_hours(
    day: date, expected_start: str, expected_end: str
) -> None:
    """ "The whole day" is local midnight to local midnight, which is 23 or 25
    hours on a transition date — never 24.

    Found by review: the only other whole-day test uses Lagos, which has never
    observed DST, so it cannot tell this apart from adding a flat 24 hours to
    the start. That mistake would leave the last local hour of a blocked day
    bookable in November and over-block an hour of the next day in March.
    """
    result = project(
        rules=[],
        exceptions=[block(start_date=day, tz=NEW_YORK, kind=AvailabilityExceptionType.OVERRIDE)],
        **one_day(day),
    )

    assert result == (UtcInterval(utc(expected_start), utc(expected_end)),)


def test_a_cross_zone_block_applies_whatever_the_query_window_is() -> None:
    """The answer must come from the data, not from how it was asked for.

    A whole-day New York block runs 05:00Z to 05:00Z the next day, so it really
    does overlap a Kolkata window sitting on the following calendar date.
    Clipping exceptions to the projection's date range dropped it, and the same
    mentor was then available or not depending on the caller's window.
    """
    # Monday 00:00-06:00 Kolkata on 2026-03-09 is 2026-03-08 18:30Z .. 00:30Z,
    # and the whole-day New York block is 2026-03-08 05:00Z .. 03-09 04:00Z.
    # They overlap in real time while sitting on different calendar dates.
    kolkata_rule = WeeklyWindow(
        day_of_week=MONDAY,
        start_time=time.fromisoformat("00:00"),
        end_time=time.fromisoformat("06:00"),
        timezone="Asia/Kolkata",
    )
    ny_block = block(start_date=date(2026, 3, 8), tz=NEW_YORK)

    unblocked = project(rules=[kolkata_rule], exceptions=[], **one_day(date(2026, 3, 9)))
    narrow = project(rules=[kolkata_rule], exceptions=[ny_block], **one_day(date(2026, 3, 9)))
    wide = project(
        rules=[kolkata_rule],
        exceptions=[ny_block],
        start=date(2026, 3, 8),
        end=date(2026, 3, 11),
    )

    # Without the block there is availability, so the two below are not empty
    # for some unrelated reason.
    assert unblocked == (UtcInterval(utc("2026-03-08T18:30:00"), utc("2026-03-09T00:30:00")),)
    assert narrow == ()
    assert wide == ()


def test_a_zero_length_block_does_not_split_a_window() -> None:
    """A block declared inside the spring-forward gap is not a boundary.

    02:00-03:00 New York on that date resolves to 07:00Z-07:00Z. Treated as a
    real block it cuts the window into two adjacent halves, which contradicts
    the contract that the result needs no further normalising.
    """
    result = project(
        rules=[window(day=SUNDAY, start="00:00", end="06:00", tz=NEW_YORK)],
        exceptions=[block(start_date=SPRING_FORWARD, start="02:00", end="03:00", tz=NEW_YORK)],
        **one_day(SPRING_FORWARD),
    )

    assert len(result) == 1


def test_a_fold_carrying_input_still_resolves_to_the_first_occurrence() -> None:
    """`datetime.combine` inherits `fold` from the `time` it is given, so a
    caller could otherwise hand in `time(1, 30, fold=1)` and silently get the
    second occurrence — RFC 5545 violated by an argument nobody looked at."""
    result = project(
        rules=[
            WeeklyWindow(
                day_of_week=SUNDAY,
                start_time=time(1, 30, fold=1),
                end_time=time(1, 45, fold=1),
                timezone=NEW_YORK,
            )
        ],
        exceptions=[],
        **one_day(FALL_BACK),
    )

    assert result[0].start == utc("2026-11-01T05:30:00")


def test_a_half_hour_offset_zone_projects_correctly() -> None:
    """`Asia/Kolkata` is UTC+05:30. Legacy `timeZone` is per row and free text,
    so a non-hour offset is a shape the migrated data can contain."""
    result = project(
        rules=[
            WeeklyWindow(
                day_of_week=MONDAY,
                start_time=time.fromisoformat("09:00"),
                end_time=time.fromisoformat("12:00"),
                timezone="Asia/Kolkata",
            )
        ],
        exceptions=[],
        **one_day(date(2026, 3, 9)),
    )

    assert result == (UtcInterval(utc("2026-03-09T03:30:00"), utc("2026-03-09T06:30:00")),)


def test_two_rules_in_different_zones_may_overlap_in_real_time_and_merge() -> None:
    """The exclusion constraint compares **wall clock** and carries no zone, so
    two rules can satisfy it and still overlap as instants. Merging is therefore
    load-bearing for rules too, not only for overrides."""
    result = project(
        rules=[
            window(day=MONDAY, start="09:00", end="12:00", tz=NEW_YORK),  # 13:00-16:00Z
            window(day=MONDAY, start="13:00", end="16:00", tz=LAGOS),  # 12:00-15:00Z
        ],
        exceptions=[],
        **one_day(date(2026, 3, 9)),
    )

    assert result == (UtcInterval(utc("2026-03-09T12:00:00"), utc("2026-03-09T16:00:00")),)


@pytest.mark.parametrize(
    ("start", "end"),
    [(date(2026, 3, 9), date(2026, 3, 9)), (date(2026, 3, 10), date(2026, 3, 9))],
    ids=["empty-range", "inverted-range"],
)
def test_a_range_with_no_days_yields_nothing(start: date, end: date) -> None:
    """An empty or inverted range is a caller's question with no days in it, not
    an error — and never an unbounded loop."""
    assert project(rules=[window(day=MONDAY)], exceptions=[], start=start, end=end) == ()


def test_an_override_adds_a_window_no_rule_describes() -> None:
    result = project(
        rules=[],
        exceptions=[
            block(
                start_date=date(2026, 3, 10),
                start="14:00",
                end="16:00",
                tz=LAGOS,
                kind=AvailabilityExceptionType.OVERRIDE,
            )
        ],
        **one_day(date(2026, 3, 10)),
    )

    assert result == (UtcInterval(utc("2026-03-10T13:00:00"), utc("2026-03-10T15:00:00")),)


def test_a_block_beats_an_override_on_the_same_date() -> None:
    """Settled: a mentor marking themselves away is never silently overridden.

    The cost of being wrong this way is a mentor with no booking. The other way
    it is a mentor booked on their holiday.
    """
    result = project(
        rules=[],
        exceptions=[
            block(
                start_date=date(2026, 3, 10),
                start="14:00",
                end="16:00",
                tz=LAGOS,
                kind=AvailabilityExceptionType.OVERRIDE,
            ),
            block(start_date=date(2026, 3, 10), tz=LAGOS),
        ],
        **one_day(date(2026, 3, 10)),
    )

    assert result == ()


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------


def test_an_override_overlapping_a_rule_merges_into_one_interval() -> None:
    """An override legitimately overlaps a rule and nothing prevents it.

    An earlier version of this docstring claimed rules could not overlap each
    other because of the exclusion constraint. That is wrong: the constraint
    compares **wall clock** and carries no timezone column, so two rules in
    different zones can satisfy it and still overlap as instants — see
    `test_two_rules_in_different_zones_may_overlap_in_real_time_and_merge`.
    """
    result = project(
        rules=[window(day=MONDAY, start="09:00", end="12:00", tz=LAGOS)],
        exceptions=[
            block(
                start_date=date(2026, 3, 9),
                start="11:00",
                end="14:00",
                tz=LAGOS,
                kind=AvailabilityExceptionType.OVERRIDE,
            )
        ],
        **one_day(date(2026, 3, 9)),
    )

    assert result == (UtcInterval(utc("2026-03-09T08:00:00"), utc("2026-03-09T13:00:00")),)


def test_two_adjacent_windows_merge() -> None:
    """09:00-12:00 and 12:00-14:00 touch, so the answer is one window."""
    result = project(
        rules=[
            window(day=MONDAY, start="09:00", end="12:00", tz=LAGOS),
            window(day=MONDAY, start="12:00", end="14:00", tz=LAGOS),
        ],
        exceptions=[],
        **one_day(date(2026, 3, 9)),
    )

    assert result == (UtcInterval(utc("2026-03-09T08:00:00"), utc("2026-03-09T13:00:00")),)


def test_the_result_is_sorted_and_non_overlapping() -> None:
    """Whatever order the caller supplies. Downstream subtraction of sessions
    and free/busy is only sane over a normalised set."""
    result = project(
        rules=[
            window(day=MONDAY, start="14:00", end="17:00", tz=LAGOS),
            window(day=MONDAY, start="09:00", end="12:00", tz=LAGOS),
        ],
        exceptions=[],
        **one_day(date(2026, 3, 9)),
    )

    assert [interval.start for interval in result] == sorted(i.start for i in result)
    assert result[0].end <= result[1].start


def test_a_multi_day_range_returns_every_matching_weekday() -> None:
    result = project(
        rules=[window(day=MONDAY, tz=LAGOS)],
        exceptions=[],
        start=date(2026, 3, 1),
        end=date(2026, 3, 29),
    )

    assert len(result) == 4  # Mondays: 2, 9, 16, 23 March


@pytest.mark.parametrize("field", ["is_active", "deleted"])
def test_the_caller_filters_inactive_rules_not_the_projection(field: str) -> None:
    """`is_active` and `deleted_at` are columns, not domain concepts.

    The projection takes the windows it is given and has no opinion about why a
    row was excluded — that is a query concern, and the partial index on
    `availability_rules` is what serves it. Asserted so nobody adds the filter
    here as well and creates a second place the rule lives.
    """
    assert not hasattr(WeeklyWindow, field)
