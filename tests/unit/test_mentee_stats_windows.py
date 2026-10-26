"""The mentee dashboard's windows (decision 249)."""

from __future__ import annotations

import datetime as dt

import pytest

from app.domain.mentee_stats import dashboard_windows
from app.domain.mentor_stats import Window

NOW = dt.datetime(2026, 3, 1, 9, 30, tzinfo=dt.UTC)


def test_the_trend_is_month_over_month_and_missed_reaches_back_90_days() -> None:
    windows = dashboard_windows(NOW)

    assert windows.current == Window(since=NOW - dt.timedelta(days=30), until=NOW)
    assert windows.previous == Window(
        since=NOW - dt.timedelta(days=60), until=windows.current.since
    )
    assert windows.missed == Window(since=NOW - dt.timedelta(days=90), until=NOW)


def test_a_naive_now_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        dashboard_windows(NOW.replace(tzinfo=None))


def test_this_month_is_the_utc_calendar_month_now_is_in() -> None:
    """From the 1st at midnight UTC to the next 1st, whatever the time of day,
    including on the 1st itself."""
    assert dashboard_windows(NOW).month == Window(
        since=dt.datetime(2026, 3, 1, tzinfo=dt.UTC), until=dt.datetime(2026, 4, 1, tzinfo=dt.UTC)
    )
    late = dt.datetime(2026, 12, 31, 23, 59, tzinfo=dt.UTC)
    assert dashboard_windows(late).month.until == dt.datetime(2027, 1, 1, tzinfo=dt.UTC)
