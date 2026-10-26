"""The windows a mentee's dashboard home is measured over (decision 249).

Built from `mentor_stats.trend_windows`, so the mentee's month and the mentor's
window are the same half-open `[since, until)` in UTC and cannot disagree about
which edge is in.

**Fixed lengths, no query parameter.** The home screen shows "this month
against last" and "missed in the last 90 days"; adding a `window_days` later is
not breaking, taking one away would be.
"""

from __future__ import annotations

import datetime as dt
from typing import NamedTuple

from app.domain.mentor_stats import Window, month_windows, trend_windows, window_ending

__all__ = ["MISSED_DAYS", "TREND_DAYS", "DashboardWindows", "dashboard_windows"]

#: Month over month: `current` is the last 30 days, `previous` the 30 before.
TREND_DAYS = 30
#: How far back the mentee's own missed sessions are counted.
MISSED_DAYS = 90


class DashboardWindows(NamedTuple):
    current: Window
    previous: Window
    missed: Window
    #: The UTC calendar month ``now`` is in, to its end: the month the mentor's
    #: monthly chart and the credit reset use (Mentee Home's "+2 this month").
    month: Window


def dashboard_windows(now: dt.datetime) -> DashboardWindows:
    """The trend's two windows ending at ``now``, and the missed window ending
    there too, and the calendar month it falls in. Refuses a naive ``now``, as
    `window_ending` does."""
    current, previous = trend_windows(now, TREND_DAYS)
    (month,) = month_windows(now, 1)
    return DashboardWindows(
        current=current,
        previous=previous,
        missed=window_ending(now, MISSED_DAYS),
        month=month,
    )
