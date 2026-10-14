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
    "Window",
    "month_windows",
    "trend_windows",
]

#: The dashboard's default: a quarter, long enough that a mentor with a session
#: a week has a figure worth comparing.
DEFAULT_WINDOW_DAYS = 90
#: A week: below it a mentor's counts are mostly zeros and the trend is noise.
MIN_WINDOW_DAYS = 7
#: A year, so "this year against last" is askable. The previous window then
#: reaches back two years, which is still a bounded read.
MAX_WINDOW_DAYS = 365

#: The monthly chart (decision 247): a year by default, two at most, so "this
#: month against the same month last year" is askable.
DEFAULT_MONTHS = 12
MIN_MONTHS = 1
MAX_MONTHS = 24


class Window(NamedTuple):
    """`[since, until)`: `since` is inside, `until` is not."""

    since: dt.datetime
    until: dt.datetime


def trend_windows(now: dt.datetime, days: int) -> tuple[Window, Window]:
    """The window ending at `now`, and the equal one before it.

    Raises `ValueError` on a naive `now`: a window compared against
    `timestamptz` columns must name its zone, and guessing one is how a figure
    shifts by the server's offset.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    length = dt.timedelta(days=days)
    current = Window(since=now - length, until=now)
    return current, Window(since=current.since - length, until=current.since)


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
    since = now.astimezone(dt.UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    windows = [Window(since=since, until=end_of_month(since))]
    for _ in range(months - 1):
        until = windows[0].since
        windows.insert(0, Window(since=(until - dt.timedelta(days=1)).replace(day=1), until=until))
    return windows
