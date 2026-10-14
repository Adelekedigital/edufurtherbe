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

__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "MAX_WINDOW_DAYS",
    "MIN_WINDOW_DAYS",
    "Window",
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
