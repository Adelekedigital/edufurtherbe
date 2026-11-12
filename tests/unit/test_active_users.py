"""Weekly and monthly active users: the day, the once-a-day memory and the
counting rules (decision 342)."""

from __future__ import annotations

import datetime as dt
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.domain.activity import SeenToday, activity_day
from app.domain.analytics import (
    MONTHLY_ACTIVE_DAYS,
    WEEKLY_ACTIVE_DAYS,
    ActiveUsers,
    Metric,
    Period,
    active_spans,
    active_tracks,
    activity_days,
)

DAY = dt.date(2025, 3, 16)


def test_an_activity_day_is_the_utc_date() -> None:
    # 00:30 on the 16th in Lagos is still the 15th in UTC.
    lagos = dt.datetime(2025, 3, 16, 0, 30, tzinfo=ZoneInfo("Africa/Lagos"))
    assert activity_day(lagos) == dt.date(2025, 3, 15)
    assert activity_day(dt.datetime(2025, 3, 16, 23, 59, tzinfo=dt.UTC)) == DAY


def test_seen_today_remembers_a_person_for_the_day_only() -> None:
    seen, ada, bo = SeenToday(), uuid4(), uuid4()
    assert not seen.has(ada, DAY)
    seen.add(ada, DAY)
    assert seen.has(ada, DAY)
    assert not seen.has(bo, DAY)
    # A new day starts over.
    tomorrow = DAY + dt.timedelta(days=1)
    assert not seen.has(ada, tomorrow)
    seen.add(bo, tomorrow)
    assert not seen.has(ada, tomorrow)
    assert not seen.has(ada, DAY)


def test_seen_today_is_bounded() -> None:
    seen, people = SeenToday(limit=2), [uuid4() for _ in range(3)]
    seen.add(people[0], DAY)
    seen.add(people[1], DAY)
    assert seen.has(people[0], DAY) and seen.has(people[1], DAY)
    seen.add(people[2], DAY)
    assert seen.has(people[2], DAY)
    assert not seen.has(people[0], DAY)


def test_the_spans_end_with_the_window_and_the_previous_period() -> None:
    period = Period(first=DAY - dt.timedelta(days=2), last=DAY, tz="Africa/Lagos")
    week, week_before = active_spans(period, WEEKLY_ACTIVE_DAYS)
    month, month_before = active_spans(period, MONTHLY_ACTIVE_DAYS)
    assert (week.first, week.last) == (dt.date(2025, 3, 10), DAY)
    # The previous period is the 3 days before: it ends on the 13th.
    assert (week_before.first, week_before.last) == (dt.date(2025, 3, 7), dt.date(2025, 3, 13))
    assert (month.first, month.last) == (dt.date(2025, 2, 15), DAY)
    assert (month_before.first, month_before.last) == (dt.date(2025, 2, 12), dt.date(2025, 3, 13))
    assert week.tz == "Africa/Lagos"


def test_stickiness_is_weekly_over_monthly_and_null_with_nobody_in_the_month() -> None:
    assert ActiveUsers(weekly=2, monthly=5).stickiness == 40.0
    assert ActiveUsers(weekly=1, monthly=3).stickiness == 33.3
    assert ActiveUsers(weekly=3, monthly=3).stickiness == 100.0
    assert ActiveUsers(weekly=0, monthly=0).stickiness is None


def test_the_tracks_compare_or_not() -> None:
    now, before = ActiveUsers(weekly=2, monthly=5), ActiveUsers(weekly=3, monthly=5)
    assert active_tracks(now, before, compare=True) == {
        "weekly_active_users": Metric(value=2, previous=3),
        "monthly_active_users": Metric(value=5, previous=5),
        "stickiness": Metric(value=40.0, previous=60.0),
    }
    assert active_tracks(now, ActiveUsers(0, 0), compare=False) == {
        "weekly_active_users": Metric(value=2, previous=None),
        "monthly_active_users": Metric(value=5, previous=None),
        "stickiness": Metric(value=40.0, previous=None),
    }


def test_the_activity_days_are_every_utc_date_overlapping_the_local_days() -> None:
    day = Period(first=DAY, last=DAY)
    assert activity_days(day) == (DAY, DAY)
    # 16 March in New York (UTC-4) runs 04:00 UTC on the 16th to 04:00 on the 17th.
    assert activity_days(Period(first=DAY, last=DAY, tz="America/New_York")) == (
        DAY,
        dt.date(2025, 3, 17),
    )
    # In Lagos (UTC+1) it starts at 23:00 UTC on the 15th.
    assert activity_days(Period(first=DAY, last=DAY, tz="Africa/Lagos")) == (
        dt.date(2025, 3, 15),
        DAY,
    )
