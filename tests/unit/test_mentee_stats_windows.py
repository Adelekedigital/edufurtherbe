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
