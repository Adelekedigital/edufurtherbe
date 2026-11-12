"""Weekly and monthly active users: the day, the once-a-day memory and the
counting rules (decision 342)."""

from __future__ import annotations

import datetime as dt
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.api.schemas.admin_analytics import ActivePointRead, WeeklyActiveRead
from app.domain.activity import SeenToday, activity_day
from app.domain.analytics import (
    MONTHLY_ACTIVE_DAYS,
    WEEKLY_ACTIVE_DAYS,
    ActiveMetrics,
    ActivePoint,
    ActiveUsers,
    Metric,
    Period,
    RoleCounts,
    WeeklyActive,
    active_metrics,
    active_series,
    active_spans,
    active_tracks,
    activity_days,
    bucket_weeks,
    everybody,
    history_from,
    series_buckets,
)
from app.domain.enums import PrimaryRole

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


# --- Active members: by role and the weekly trend (decision 343) ------------


def test_a_roles_three_figures_compare_or_not_and_stickiness_needs_a_month() -> None:
    now, before = ActiveUsers(weekly=1, monthly=4), ActiveUsers(weekly=0, monthly=0)
    assert active_metrics(now, before, compare=True) == ActiveMetrics(
        weekly=Metric(value=1, previous=0),
        monthly=Metric(value=4, previous=0),
        stickiness=Metric(value=25.0, previous=None),
    )
    assert active_metrics(now, ActiveUsers(2, 2), compare=False) == ActiveMetrics(
        weekly=Metric(value=1, previous=None),
        monthly=Metric(value=4, previous=None),
        stickiness=Metric(value=25.0, previous=None),
    )


def test_each_daily_point_reads_the_7_days_ending_that_day() -> None:
    period = Period(first=dt.date(2025, 3, 1), last=dt.date(2025, 3, 14), tz="Africa/Lagos")
    weeks = bucket_weeks(period)
    starts = [period.day_of(bucket.since) for bucket in series_buckets(period)]
    assert [start for start, _ in weeks] == starts
    first_start, first_span = weeks[0]
    assert first_start == dt.date(2025, 3, 1)
    assert (first_span.first, first_span.last) == (dt.date(2025, 2, 23), dt.date(2025, 3, 1))
    assert first_span.tz == "Africa/Lagos"
    assert all(span.days == WEEKLY_ACTIVE_DAYS for _, span in weeks)


def test_each_weekly_point_reads_the_7_days_ending_its_last_day() -> None:
    # 90 days: 12 full weeks and a last bucket of 6 days.
    period = Period(first=dt.date(2025, 1, 1), last=dt.date(2025, 3, 31))
    weeks = bucket_weeks(period)
    assert len(weeks) == 13
    assert [start for start, _ in weeks] == [
        period.day_of(bucket.since) for bucket in series_buckets(period)
    ]
    start, span = weeks[0]
    assert (start, span.first, span.last) == (
        dt.date(2025, 1, 1),
        dt.date(2025, 1, 1),
        dt.date(2025, 1, 7),
    )
    start, span = weeks[-1]
    assert (start, span.first, span.last) == (
        dt.date(2025, 3, 26),
        dt.date(2025, 3, 25),
        dt.date(2025, 3, 31),
    )


Pair = tuple[int | None, int | None]


def counts(_total: list[int], mentee: list[int], mentor: list[int]) -> RoleCounts:
    """Per-point counts for each role, as the store reads them; the total is
    written beside them for the reader, and derived (decision 343)."""
    return {PrimaryRole.MENTEE: mentee, PrimaryRole.MENTOR: mentor}


def point(day: dt.date, total: Pair, mentee: Pair, mentor: Pair) -> ActivePoint:
    return ActivePoint(
        start=day,
        total=WeeklyActive(*total),
        by_role={
            PrimaryRole.MENTEE: WeeklyActive(*mentee),
            PrimaryRole.MENTOR: WeeklyActive(*mentor),
        },
    )


NONE: Pair = (None, None)


def test_a_point_is_null_until_its_week_starts_on_a_full_recorded_day() -> None:
    # Recording began part-way through 20 Feb: the 21st is the first full day.
    period = Period(first=dt.date(2025, 2, 26), last=dt.date(2025, 2, 27))
    earliest = dt.date(2025, 2, 20)
    now, before = counts([5, 6], [3, 4], [2, 2]), counts([7, 8], [7, 5], [0, 3])
    # The 26th reads 20-26 Feb (starts on the partial day); the 27th reads 21-27.
    # A null point has null roles too.
    assert active_series(period, now, before, earliest, compare=True) == [
        point(dt.date(2025, 2, 26), NONE, NONE, NONE),
        point(dt.date(2025, 2, 27), (6, None), (4, None), (2, None)),
    ]
    later = Period(first=dt.date(2025, 3, 10), last=dt.date(2025, 3, 11))
    assert active_series(later, now, before, earliest, compare=True) == [
        point(dt.date(2025, 3, 10), (5, 7), (3, 7), (2, 0)),
        point(dt.date(2025, 3, 11), (6, 8), (4, 5), (2, 3)),
    ]


def test_without_compare_only_previous_is_null() -> None:
    period = Period(first=dt.date(2025, 3, 10), last=dt.date(2025, 3, 11))
    now, before = counts([5, 0], [4, 0], [1, 0]), counts([7, 8], [7, 5], [0, 3])
    assert active_series(period, now, before, dt.date(2025, 1, 1), compare=False) == [
        point(dt.date(2025, 3, 10), (5, None), (4, None), (1, None)),
        point(dt.date(2025, 3, 11), (0, None), (0, None), (0, None)),
    ]


def test_with_nothing_recorded_every_point_is_null() -> None:
    period = Period(first=dt.date(2025, 3, 10), last=dt.date(2025, 3, 11))
    zero = counts([0, 0], [0, 0], [0, 0])
    assert active_series(period, zero, zero, None, compare=True) == [
        point(dt.date(2025, 3, 10), NONE, NONE, NONE),
        point(dt.date(2025, 3, 11), NONE, NONE, NONE),
    ]


def test_the_null_rule_reads_the_weeks_first_utc_date() -> None:
    # In Lagos (UTC+1) 21 Feb starts at 23:00 UTC on the 20th, the partial day.
    earliest = dt.date(2025, 2, 20)
    now, before = counts([1, 1], [1, 1], [0, 0]), counts([0, 0], [0, 0], [0, 0])
    lagos = Period(first=dt.date(2025, 2, 27), last=dt.date(2025, 2, 28), tz="Africa/Lagos")
    assert [p.total.value for p in active_series(lagos, now, before, earliest, True)] == [None, 1]
    # In New York the 21st starts at 05:00 UTC on the 21st: already full.
    york = Period(first=dt.date(2025, 2, 27), last=dt.date(2025, 2, 28), tz="America/New_York")
    assert [p.total.value for p in active_series(york, now, before, earliest, True)] == [1, 1]


def test_everybody_is_mentees_plus_mentors() -> None:
    by_role = {
        PrimaryRole.MENTEE: ActiveUsers(weekly=2, monthly=5),
        PrimaryRole.MENTOR: ActiveUsers(weekly=1, monthly=3),
    }
    assert everybody(by_role) == ActiveUsers(weekly=3, monthly=8)


def test_a_point_reads_its_own_fields_and_not_a_role_pairs() -> None:
    assert not issubclass(ActivePointRead, WeeklyActiveRead)
    day = dt.date(2025, 3, 10)
    read = ActivePointRead.of(point(day, (5, None), (3, None), (2, None)))
    assert read.model_dump(mode="json") == {
        "start": "2025-03-10",
        "value": 5,
        "previous": None,
        "by_role": {
            "mentee": {"value": 3, "previous": None},
            "mentor": {"value": 2, "previous": None},
        },
    }


def test_history_starts_the_day_after_the_first_recorded_day() -> None:
    assert history_from(None) is None
    assert history_from(dt.date(2025, 2, 20)) == dt.date(2025, 2, 21)


def test_the_first_shown_point_starts_on_or_after_history_from() -> None:
    earliest = dt.date(2025, 2, 20)
    period = Period(first=dt.date(2025, 2, 20), last=dt.date(2025, 3, 5))
    ones = counts([], [1] * period.days, [0] * period.days)
    points = active_series(period, ones, ones, earliest, compare=True)
    spans = dict(bucket_weeks(period))
    shown = [p.start for p in points if p.total.value is not None]
    first_full = history_from(earliest)
    assert first_full is not None
    assert spans[shown[0]].first == first_full
    # The point before it reads a week starting on the partial day.
    assert spans[shown[0] - dt.timedelta(days=1)].first < first_full
