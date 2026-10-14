"""The dashboard's two windows (decision 246)."""

from __future__ import annotations

import datetime as dt

import pytest

from app.domain.mentor_stats import Window, trend_windows, window_ending

NOW = dt.datetime(2026, 3, 1, 9, 30, tzinfo=dt.UTC)


def test_current_ends_now_and_previous_ends_where_current_starts() -> None:
    current, previous = trend_windows(NOW, 30)

    assert current == Window(since=NOW - dt.timedelta(days=30), until=NOW)
    assert previous == Window(since=NOW - dt.timedelta(days=60), until=current.since)


def test_a_naive_now_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        trend_windows(NOW.replace(tzinfo=None), 30)


def test_window_ending_is_the_days_before_now() -> None:
    assert window_ending(NOW, 90) == Window(since=NOW - dt.timedelta(days=90), until=NOW)


def test_window_ending_refuses_a_naive_now() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        window_ending(NOW.replace(tzinfo=None), 90)
