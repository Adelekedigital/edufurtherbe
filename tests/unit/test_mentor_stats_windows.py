"""The dashboard's two windows (decision 246)."""

from __future__ import annotations

import datetime as dt
from itertools import pairwise

import pytest

from app.domain.mentor_stats import (
    YEAR_REACH,
    Window,
    month_windows,
    trend_windows,
    window_ending,
    year_windows,
)

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


def test_a_year_is_its_utc_calendar_year_and_the_one_before() -> None:
    """Decision 257: `previous` is the calendar year before, not the same
    length before, so a leap year is still compared with a whole year."""
    current, previous = year_windows(NOW, 2025)

    assert current == Window(since=utc(2025, 1), until=utc(2026, 1))
    assert previous == Window(since=utc(2024, 1), until=utc(2025, 1))


def test_the_current_year_runs_to_its_end() -> None:
    assert year_windows(NOW, 2026)[0] == Window(since=utc(2026, 1), until=utc(2027, 1))


def test_a_year_reaches_as_far_back_as_the_monthly_chart() -> None:
    assert YEAR_REACH == 4
    assert year_windows(NOW, 2026 - YEAR_REACH)[0].since == utc(2022, 1)


@pytest.mark.parametrize("year", [2026 - YEAR_REACH - 1, 2027])
def test_a_year_outside_the_reach_is_refused(year: int) -> None:
    with pytest.raises(ValueError, match="year"):
        year_windows(NOW, year)


def test_the_year_is_read_in_utc() -> None:
    """New Year's Eve evening in New York (UTC-5) is already next year in UTC."""
    eve = dt.datetime(2026, 12, 31, 23, 30, tzinfo=dt.timezone(dt.timedelta(hours=-5)))

    assert year_windows(eve, 2027)[0].since == utc(2027, 1)


def test_a_naive_now_is_refused_for_a_year() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        year_windows(NOW.replace(tzinfo=None), 2026)
