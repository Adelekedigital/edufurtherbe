"""Recurring availability projected onto real instants.

A mentor declares a **wall-clock** window — "Mondays, 09:00 to 12:00, Lagos" —
which is not an instant and does not become one until a date is named. This
module names the dates and does the conversion, and it is the only place in the
codebase that turns declared availability into UTC.

**RFC 5545 §3.3.5 is the specification**, because it is what every calendar the
mentor already uses implements. It has exactly two rules for the awkward cases:

    a local time occurring twice   -> the FIRST occurrence
    a local time that never occurs -> the offset BEFORE the gap

Python's ``fold=0`` — the default — is precisely this. Nothing here asks for it,
which is why ``test_fold_zero_is_rfc_5545`` pins the equivalence: the wrong
behaviour is one keyword away and would look entirely reasonable.

**Both endpoints resolve independently, so duration is not preserved.** A
09:00-17:00 window contains seven real hours on the day the clocks go forward
and nine on the day they go back, and that is correct — the mentor's day genuinely
is shorter or longer. Preserving the span instead would move the window's end
past the time they declared. A *session* is the opposite case and is stored as a
``timestamptz`` instant precisely so it stays sixty minutes.

**Everything is UTC from here on.** Subtracting exceptions, and later confirmed
sessions and Google free/busy, all happen on the intervals this returns.
Interval arithmetic in local time is how DST bugs are written.

Nothing in this module knows about a viewer, a request, or a database row: the
API returns these instants and the client renders them in whoever's zone is
looking. A local time formatted on the server is what
``12hr-localStartTime-TXT`` was, and it disagrees with the stored time by five
hours on half the legacy rows.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.domain.enums import AvailabilityExceptionType

__all__ = [
    "DatedException",
    "UnknownTimezoneError",
    "UtcInterval",
    "WeeklyWindow",
    "normalise_timezone",
    "project",
]


class UnknownTimezoneError(ValueError):
    """A timezone that is not in the tz database.

    A ``ValueError`` subclass on purpose: Pydantic turns one into a 422 without
    the API layer knowing anything about this module, and the ETL's
    ``_resolve_timezone`` wraps it with the Bubble id it has and the API does
    not.
    """


def normalise_timezone(name: str) -> str:
    """A trimmed IANA name, or a refusal.

    **The single check in the project.** ``availability_rules.timezone`` and
    ``availability_exceptions.timezone`` are `text` with no CHECK, because
    `pg_timezone_names` is not immutable and PostgreSQL will not accept it in
    one — so there is nothing behind this. An unrecognised value passes NOT NULL
    and then raises inside ``project`` when somebody's availability is rendered,
    which is a long way from whoever typed it.

    Trimmed as well as validated: ``" America/New_York "`` is a name `ZoneInfo`
    rejects, and storing it unstripped would defer the same failure.
    """
    trimmed = name.strip()
    try:
        ZoneInfo(trimmed)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise UnknownTimezoneError(f"{name!r} is not an IANA timezone") from exc
    return trimmed


@dataclass(frozen=True, slots=True, order=True)
class UtcInterval:
    """A half-open span of real time, ``[start, end)``.

    Half-open so that a window ending at 12:00 and one starting at 12:00 are
    adjacent rather than overlapping — the same reason the ``timerange`` in the
    exclusion constraint and the ``daterange`` on exceptions are both ``[)``.
    """

    start: dt.datetime
    end: dt.datetime


@dataclass(frozen=True, slots=True)
class WeeklyWindow:
    """One recurring weekly window, as declared.

    Deliberately **not** the ORM row: ``domain`` imports no other layer, and the
    projection has no opinion on ``is_active`` or ``deleted_at``. Those are
    columns, filtered by the query that fetches the rows — the partial index on
    ``availability_rules`` exists for exactly that. Repeating the filter here
    would put one rule in two places.
    """

    #: 0 = Sunday, matching the legacy ``dayOfWeekIn``.
    day_of_week: int
    #: ``end_time`` must be strictly later than ``start_time`` on the clock: a
    #: window crossing midnight is stored as two rows on two weekdays, and the
    #: database refuses the alternative (``CHECK (end_time > start_time)``).
    #: This type is also constructed from ``scripts/``, which no gate checks, so
    #: the precondition is stated rather than assumed — a reversed window
    #: resolves to a negative span and is dropped silently.
    start_time: dt.time
    end_time: dt.time
    #: IANA name. Per row, because the legacy column was.
    timezone: str


@dataclass(frozen=True, slots=True)
class DatedException:
    """A date range on which the weekly rules do not apply as written.

    ``start_date`` is inclusive and ``end_date`` exclusive, matching the
    ``daterange [)`` the rows are stored in. Null times mean the whole day —
    which is every migrated row, since legacy ``CalendarExtra`` carries only
    dates.
    """

    kind: AvailabilityExceptionType
    start_date: dt.date
    end_date: dt.date
    timezone: str
    start_time: dt.time | None = None
    end_time: dt.time | None = None


def project(
    *,
    rules: Sequence[WeeklyWindow],
    exceptions: Sequence[DatedException],
    start: dt.date,
    end: dt.date,
) -> tuple[UtcInterval, ...]:
    """Declared availability over ``[start, end)``, as UTC intervals.

    Overrides are added to whatever the rules describe; blocks are then removed
    from the union. **Blocks are applied last and therefore win**, which is the
    settled precedence: a mentor marking themselves away is never silently
    overridden by an older one-off, and the cost of being wrong that way is a
    mentor with no booking rather than a mentor booked on their holiday.

    The returned intervals are sorted, non-overlapping and non-empty. Nothing
    downstream has to normalise again, and subtracting sessions or free/busy
    from an arbitrary set is materially harder than from this one.
    """
    available = [
        *_from_rules(rules, start, end),
        *_from_exceptions(exceptions, AvailabilityExceptionType.OVERRIDE, start, end),
    ]
    blocked = _from_exceptions(exceptions, AvailabilityExceptionType.BLOCK, start, end)
    return tuple(_subtract(_merge(available), _merge(blocked)))


# --------------------------------------------------------------------------
# Resolution — wall clock to instant
# --------------------------------------------------------------------------


def _dates(start: dt.date, end: dt.date) -> Iterator[dt.date]:
    for offset in range(max((end - start).days, 0)):
        yield start + dt.timedelta(days=offset)


def _resolve(day: dt.date, moment: dt.time, zone: ZoneInfo) -> dt.datetime:
    """One wall-clock time on one date, as an instant.

    ``fold=0`` is RFC 5545's rule for both a repeated and a nonexistent local
    time, and it is normalised here rather than inherited. ``datetime.combine``
    takes ``fold`` from the ``time`` it is given, so a caller constructing
    ``time(1, 30, fold=1)`` would silently get the *second* occurrence of an
    ambiguous hour — the spec violated by an argument nobody looked at. Setting
    it enforces the rule instead of assuming it.
    """
    return dt.datetime.combine(day, moment.replace(fold=0), tzinfo=zone).astimezone(dt.UTC)


def _span(
    day: dt.date, start_time: dt.time | None, end_time: dt.time | None, timezone: str
) -> UtcInterval:
    """A window on one date, or the whole of that date when times are absent.

    "Whole day" is midnight to midnight **in the exception's own zone**, so it
    is 24 hours of local time rather than of UTC — those differ across a
    transition, and using UTC would leave an hour of a blocked day bookable.
    """
    zone = ZoneInfo(timezone)
    if start_time is None or end_time is None:
        return UtcInterval(
            _resolve(day, dt.time.min, zone),
            _resolve(day + dt.timedelta(days=1), dt.time.min, zone),
        )
    return UtcInterval(_resolve(day, start_time, zone), _resolve(day, end_time, zone))


def _from_rules(
    rules: Sequence[WeeklyWindow], start: dt.date, end: dt.date
) -> Iterator[UtcInterval]:
    for day in _dates(start, end):
        # `isoweekday()` is Monday=1..Sunday=7, so `% 7` lands Sunday on 0 —
        # the legacy `dayOfWeekIn` convention — without a lookup table.
        weekday = day.isoweekday() % 7
        for rule in rules:
            if rule.day_of_week != weekday:
                continue
            # A window resolving to zero length fell inside a spring-forward
            # gap — not a very short window, a window that does not exist.
            # It is dropped in `_subtract`, which is the single place empty
            # spans die; a guard here as well would be a second copy of one
            # rule that no test could tell apart from the first.
            yield _span(day, rule.start_time, rule.end_time, rule.timezone)


def _from_exceptions(
    exceptions: Sequence[DatedException],
    kind: AvailabilityExceptionType,
    start: dt.date,
    end: dt.date,
) -> Iterator[UtcInterval]:
    for exception in exceptions:
        if exception.kind is not kind:
            continue
        # Widened by a day at each end, deliberately. The clamp used to be the
        # calendar intersection with the projection range, which is wrong the
        # moment the exception's zone differs from the rule's: a whole-day New
        # York block covers 05:00Z to 05:00Z the next day, so it genuinely
        # overlaps a Kolkata window sitting on the *following* calendar date.
        # Clipping by date dropped it, and the answer then depended on the
        # caller's query window rather than on the data.
        #
        # A day either side is enough because no zone is more than 26 hours from
        # any other. UTC subtraction decides the rest, which is the only
        # arithmetic that can.
        for day in _dates(
            max(exception.start_date, start - dt.timedelta(days=1)),
            min(exception.end_date, end + dt.timedelta(days=1)),
        ):
            yield _span(day, exception.start_time, exception.end_time, exception.timezone)


# --------------------------------------------------------------------------
# Interval arithmetic, entirely in UTC
# --------------------------------------------------------------------------


def _merge(intervals: Iterable[UtcInterval]) -> list[UtcInterval]:
    """Sorted, with overlapping and touching spans folded together.

    **Empty spans are dropped here, and this is the only place they are.** A
    window declared inside a spring-forward gap resolves to zero length, and it
    matters on both sides: as availability it is a bookable window of no
    duration, and as a *block* it is worse — `_subtract` would treat it as a
    real boundary and cut one window into two adjacent halves, breaking the
    promise that nothing downstream has to normalise again.
    """
    merged: list[UtcInterval] = []
    for interval in sorted(intervals):
        if interval.end <= interval.start:
            continue
        if merged and interval.start <= merged[-1].end:
            merged[-1] = UtcInterval(merged[-1].start, max(merged[-1].end, interval.end))
        else:
            merged.append(interval)
    return merged


def _subtract(available: list[UtcInterval], blocked: list[UtcInterval]) -> Iterator[UtcInterval]:
    """Every part of ``available`` not covered by ``blocked``.

    One available span can produce two results — a block through the middle of a
    morning leaves a window either side of it — which is why this yields rather
    than returning one interval per input.

    **This is also where empty spans die, and it is the only place.** Both
    ``yield`` sites are guarded by a strict ``<``, so a window that resolved to
    zero length — one declared inside a spring-forward gap — cannot leave here.
    An earlier version also filtered at resolution; a mutation batch showed that
    guard was unreachable, because this function had already made it impossible
    for such an interval to escape.
    """
    for interval in available:
        cursor = interval.start
        for block in blocked:
            if block.end <= cursor or block.start >= interval.end:
                continue
            if block.start > cursor:
                yield UtcInterval(cursor, block.start)
            cursor = max(cursor, block.end)
            if cursor >= interval.end:
                break
        if cursor < interval.end:
            yield UtcInterval(cursor, interval.end)
