"""The windows a mentor's dashboard figures are measured over (decision 246).

**Half-open, in UTC.** A window is `[since, until)`, so two adjacent windows
partition time and a row on the boundary is counted once, in the later one: the
shape decision 130 gave the admin history for the same reason. A held or missed
session belongs to the instant it was booked for (`starts_at`), not to when it
was settled; a cancellation to when it was made, since cancelling a session a
month out is this week's act; a review to when it was written.

**The previous window is the same length, immediately before.** The client
computes the trend from the two blocks; publishing a percentage change would put
a division by zero and a rounding rule in the API for a figure the client can
derive exactly.
"""

from __future__ import annotations

import datetime as dt
from typing import NamedTuple

from app.domain.credits import end_of_month

__all__ = [
    "DEFAULT_MONTHS",
    "DEFAULT_WINDOW_DAYS",
    "MAX_MONTHS",
    "MAX_WINDOW_DAYS",
    "MIN_MONTHS",
    "MIN_WINDOW_DAYS",
    "YEAR_REACH",
    "Window",
    "month_windows",
    "trend_windows",
    "window_ending",
    "year_windows",
]

#: The dashboard's default: a quarter, long enough that a mentor with a session
#: a week has a figure worth comparing.
DEFAULT_WINDOW_DAYS = 90
#: A week: below it a mentor's counts are mostly zeros and the trend is noise.
MIN_WINDOW_DAYS = 7
#: A year, so "this year against last" is askable. The previous window then
#: reaches back two years, which is still a bounded read.
MAX_WINDOW_DAYS = 365

#: The monthly chart (decision 247): a year by default; five years at most, so a
#: mentor's whole history fits (the platform's first session is September 2024).
#: Longer than that wants coarser bars, not more of them (#458).
DEFAULT_MONTHS = 12
MIN_MONTHS = 1
MAX_MONTHS = 60

#: How many calendar years before the current one `year_windows` reaches: the
#: years the monthly chart shows whole (decision 257). Its `MAX_MONTHS` reach
#: back from the current month always covers the four previous calendar years
#: in full and the fifth only in part, so the fifth is not offered.
YEAR_REACH = MAX_MONTHS // 12 - 1


class Window(NamedTuple):
    """`[since, until)`: `since` is inside, `until` is not."""

    since: dt.datetime
    until: dt.datetime

    @property
    def month(self) -> str:
        """`YYYY-MM` of the UTC month `since` falls in: a monthly series' label
        (decision 247), the one place it is spelled."""
        return self.since.astimezone(dt.UTC).strftime("%Y-%m")


def window_ending(now: dt.datetime, days: int) -> Window:
    """The ``days`` before `now`: `[now - days, now)`.

    Raises `ValueError` on a naive `now`: a window compared against
    `timestamptz` columns must name its zone, and guessing one is how a figure
    shifts by the server's offset.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return Window(since=now - dt.timedelta(days=days), until=now)


def trend_windows(now: dt.datetime, days: int) -> tuple[Window, Window]:
    """The window ending at `now`, and the equal one before it."""
    current = window_ending(now, days)
    return current, window_ending(current.since, days)


def year_windows(now: dt.datetime, year: int) -> tuple[Window, Window]:
    """UTC calendar ``year`` and the calendar year before it (decision 257).

    The previous window is the year before, not the same length before, so a
    leap year is compared with a whole year. The current year runs to its end,
    as `month_windows` runs the current month to its end. Raises `ValueError`
    on a naive `now`, or a ``year`` after `now`'s or more than `YEAR_REACH`
    before it.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    latest = now.astimezone(dt.UTC).year
    if not latest - YEAR_REACH <= year <= latest:
        raise ValueError(f"year must be between {latest - YEAR_REACH} and {latest}")

    def new_year(of: int) -> dt.datetime:
        return dt.datetime(of, 1, 1, tzinfo=dt.UTC)

    return (
        Window(since=new_year(year), until=new_year(year + 1)),
        Window(since=new_year(year - 1), until=new_year(year)),
    )


def month_windows(now: dt.datetime, months: int) -> list[Window]:
    """The last ``months`` UTC calendar months, oldest first, ending with the
    month ``now`` is in (decision 247).

    **UTC, the platform's month** (`credits.end_of_month`, whose rule closes
    each month here): the windowed figures beside it are UTC too (decision
    246), so a month and a window never disagree about where a session falls.
    The current month runs to its end, not to `now`; nothing settled or
    cancelled can be placed in the part still ahead.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if months < 1:
        raise ValueError("a series needs at least one month")
    since = now.astimezone(dt.UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    windows = [Window(since=since, until=end_of_month(since))]
    for _ in range(months - 1):
        until = windows[0].since
        windows.insert(0, Window(since=(until - dt.timedelta(days=1)).replace(day=1), until=until))
    return windows
