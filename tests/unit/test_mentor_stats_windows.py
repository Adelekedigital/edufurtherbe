"""The dashboard's two windows (decision 246)."""

from __future__ import annotations

import datetime as dt
from itertools import pairwise

import pytest

from app.domain.mentor_stats import Window, month_windows, trend_windows

NOW = dt.datetime(2026, 3, 1, 9, 30, tzinfo=dt.UTC)


def test_current_ends_now_and_previous_ends_where_current_starts() -> None:
    current, previous = trend_windows(NOW, 30)

    assert current == Window(since=NOW - dt.timedelta(days=30), until=NOW)
    assert previous == Window(since=NOW - dt.timedelta(days=60), until=current.since)


def test_a_naive_now_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        trend_windows(NOW.replace(tzinfo=None), 30)


# --------------------------------------------------------------------------
# The monthly series (decision 247)
# --------------------------------------------------------------------------


def utc(year: int, month: int) -> dt.datetime:
    return dt.datetime(year, month, 1, tzinfo=dt.UTC)


def test_months_run_oldest_first_and_end_with_the_month_of_now() -> None:
    windows = month_windows(NOW, 3)

    assert windows == [
        Window(since=utc(2026, 1), until=utc(2026, 2)),
        Window(since=utc(2026, 2), until=utc(2026, 3)),
        Window(since=utc(2026, 3), until=utc(2026, 4)),
    ]


def test_months_cross_a_year() -> None:
    windows = month_windows(dt.datetime(2026, 1, 20, tzinfo=dt.UTC), 2)

    assert windows == [
        Window(since=utc(2025, 12), until=utc(2026, 1)),
        Window(since=utc(2026, 1), until=utc(2026, 2)),
    ]


def test_months_are_adjacent_across_a_leap_february() -> None:
    windows = month_windows(dt.datetime(2028, 4, 1, tzinfo=dt.UTC), 24)

    assert len(windows) == 24
    assert all(a.until == b.since for a, b in pairwise(windows))
    assert Window(since=utc(2028, 2), until=utc(2028, 3)) in windows


def test_months_are_utc_whatever_zone_now_is_in() -> None:
    """00:30 on 1 March in Lagos is still February in UTC."""
    lagos = dt.timezone(dt.timedelta(hours=1))

    windows = month_windows(dt.datetime(2026, 3, 1, 0, 30, tzinfo=lagos), 1)

    assert windows == [Window(since=utc(2026, 2), until=utc(2026, 3))]


def test_a_naive_now_is_refused_for_months() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        month_windows(NOW.replace(tzinfo=None), 12)


@pytest.mark.parametrize("months", [0, -1])
def test_fewer_than_one_month_is_refused(months: int) -> None:
    with pytest.raises(ValueError, match="at least one month"):
        month_windows(NOW, months)


def test_each_month_carries_its_utc_label() -> None:
    """The `YYYY-MM` the API publishes is the domain's, read off `since` in UTC."""
    assert [w.month for w in month_windows(NOW, 3)] == ["2026-01", "2026-02", "2026-03"]
