"""The admin Analytics rules (PRD 31, decision 324): windows, buckets, rates,
the credit source groups and the alert thresholds."""

from __future__ import annotations

import datetime as dt
import itertools
from zoneinfo import ZoneInfo

import pytest

from app.core.errors import AnalyticsWindowError
from app.domain import analytics as a
from app.domain.enums import CreditSource
from app.domain.mentor_stats import Window

TODAY = dt.date(2026, 11, 9)
NOW = dt.datetime(2026, 11, 9, 15, 0, tzinfo=dt.UTC)


def period(**ask: str | None) -> a.Period:
    return a.resolve_period(
        TODAY, preset=ask.get("window"), first=ask.get("from"), last=ask.get("to")
    )


def pointer_of(**ask: str | None) -> str:
    with pytest.raises(AnalyticsWindowError) as refused:
        period(**ask)
    return refused.value.field_errors[0][0]


@pytest.mark.parametrize(("preset", "days"), [("7d", 7), ("30d", 30), ("90d", 90)])
def test_a_preset_is_whole_days_ending_today(preset: str, days: int) -> None:
    got = period(window=preset)
    assert got.last == TODAY
    assert got.days == days


def test_nothing_asked_is_thirty_days() -> None:
    assert period() == period(window="30d")


def test_the_previous_period_is_as_long_and_ends_the_day_before() -> None:
    got = period(window="7d")
    before = got.previous()
    assert before.days == 7
    assert before.last == got.first - dt.timedelta(days=1)


def test_a_period_as_instants_is_half_open_on_utc_midnights() -> None:
    window = period(**{"from": "2026-11-03", "to": "2026-11-03"}).window
    assert window.since == dt.datetime(2026, 11, 3, tzinfo=dt.UTC)
    assert window.until == dt.datetime(2026, 11, 4, tzinfo=dt.UTC)


def test_a_custom_range_reaches_both_permitted_edges() -> None:
    got = period(**{"from": "2024-06-01", "to": TODAY.isoformat()})
    assert (got.first, got.last) == (a.EARLIEST_DAY, TODAY)


@pytest.mark.parametrize(
    ("ask", "pointer"),
    [
        ({"window": "14d"}, "/query/window"),
        ({"window": "7d", "from": "2026-11-01", "to": "2026-11-02"}, "/query/window"),
        ({"from": "2026-11-01"}, "/query/to"),
        ({"to": "2026-11-01"}, "/query/from"),
        ({"from": "2024-05-31", "to": "2026-11-01"}, "/query/from"),
        ({"from": "2026-11-01", "to": "2026-11-10"}, "/query/to"),
        ({"from": "2026-11-05", "to": "2026-11-04"}, "/query/from"),
        ({"from": "Nov 3", "to": "2026-11-04"}, "/query/from"),
        ({"from": "20261103", "to": "2026-11-04"}, "/query/from"),
        ({"from": "2026-11-03", "to": "2026-W45-3"}, "/query/to"),
    ],
)
def test_a_window_that_cannot_be_read_names_its_parameter(
    ask: dict[str, str], pointer: str
) -> None:
    assert pointer_of(**ask) == pointer


@pytest.mark.parametrize(("value", "expected"), [(None, True), ("true", True), ("false", False)])
def test_compare_reads_true_or_false(value: str | None, expected: bool) -> None:
    assert a.resolve_compare(value) is expected


def test_compare_refuses_anything_else() -> None:
    with pytest.raises(AnalyticsWindowError):
        a.resolve_compare("yes")


def test_fourteen_days_draw_by_day_and_fifteen_by_seven_days() -> None:
    fourteen = period(**{"from": "2026-10-27", "to": "2026-11-09"})
    fifteen = period(**{"from": "2026-10-26", "to": "2026-11-09"})
    assert a.series_unit(fourteen) == "day"
    assert len(a.series_buckets(fourteen)) == 14
    weeks = a.series_buckets(fifteen)
    assert a.series_unit(fifteen) == "week"
    assert [w.until - w.since for w in weeks] == [dt.timedelta(days=7)] * 2 + [dt.timedelta(days=1)]
    assert weeks[0].since == fifteen.window.since
    assert weeks[-1].until == fifteen.window.until


def test_a_sparkline_is_eight_equal_parts_covering_the_window() -> None:
    window = period(window="7d").window
    parts = a.spark_buckets(window)
    assert len(parts) == a.SPARK_POINTS
    assert parts[0].since == window.since
    assert parts[-1].until == window.until
    assert all(x.until == y.since for x, y in itertools.pairwise(parts))


def test_a_rate_rounds_half_away_from_zero_and_is_none_over_nothing() -> None:
    # 1/16 = 6.25% sits exactly on a tie at one decimal.
    assert a.rate(1, 16) == 6.3
    assert a.rate(5, 8) == 62.5
    assert a.rate(0, 4) == 0.0
    assert a.rate(3, 0) is None
    assert a.ratio(5, 3) == 1.67
    assert a.ratio(1, 0) is None


def test_every_lot_source_is_in_exactly_one_credit_group() -> None:
    assert set(a.SOURCE_GROUP) == set(CreditSource)
    assert set(a.SOURCE_GROUP.values()) == set(a.CREDIT_GROUPS)


def test_quiet_for_six_days_raises_nothing() -> None:
    # Owner, 2026-11-12: the window is a week, not a day.
    for key in ("no_booking_requests", "no_signups"):
        assert a.quiet_alert(key, NOW - dt.timedelta(days=6), NOW) is None
        last = NOW - dt.timedelta(days=7) + dt.timedelta(seconds=1)
        assert a.quiet_alert(key, last, NOW) is None


def test_quiet_for_seven_days_raises_when_the_quiet_began() -> None:
    last = NOW - dt.timedelta(days=7)
    alert = a.quiet_alert("no_signups", last, NOW)
    assert alert is not None
    assert alert.raised_at == NOW
    assert alert.title == "No sign-ups"
    assert alert.body == "No one has signed up in the last 7 days."
    assert alert.link == a.AlertLink(tab="funnels", funnel="signup")


def test_quiet_for_more_than_seven_days_raised_seven_days_after_the_last() -> None:
    last = NOW - dt.timedelta(days=9)
    alert = a.quiet_alert("no_booking_requests", last, NOW)
    assert alert is not None
    assert alert.raised_at == last + dt.timedelta(days=7)
    assert alert.title == "No booking requests"
    assert alert.body == "No one has asked for a session in the last 7 days."
    assert alert.link == a.AlertLink(tab="sessions")


def test_nothing_ever_raises_now() -> None:
    alert = a.quiet_alert("no_booking_requests", None, NOW)
    assert alert is not None
    assert alert.raised_at == NOW
    assert alert.link == a.AlertLink(tab="sessions")


def test_delivery_fires_above_twenty_only() -> None:
    assert a.delivery_alert(a.DELIVERY_FAILED_OVER, TODAY) is None
    alert = a.delivery_alert(a.DELIVERY_FAILED_OVER + 1, TODAY)
    assert alert is not None
    assert alert.body == "21 emails, notifications or analytics events failed yesterday."
    assert alert.link is None


def test_alerts_come_newest_first() -> None:
    old = a.quiet_alert("no_signups", NOW - dt.timedelta(days=10), NOW)
    new = a.quiet_alert("no_booking_requests", NOW - dt.timedelta(days=8), NOW)
    assert [x.key for x in a.newest_first([old, None, new])] == [
        "no_booking_requests",
        "no_signups",
    ]


def test_a_cached_tab_lives_until_cache_for_after_it_was_put() -> None:
    cache = a.TabCache()
    cache.put(("sessions", 1), "figures", NOW)
    assert cache.get(("sessions", 1), NOW + a.CACHE_FOR - dt.timedelta(seconds=1)) == "figures"
    assert cache.get(("sessions", 1), NOW + a.CACHE_FOR) is None
    assert cache.get(("sessions", 2), NOW) is None


def test_the_cache_drops_its_oldest_entry_past_its_size() -> None:
    cache = a.TabCache()
    for n in range(a.CACHE_SIZE + 1):
        cache.put(("t", n), n, NOW)
    assert cache.get(("t", 0), NOW) is None
    assert cache.get(("t", 1), NOW) == 1
    assert cache.get(("t", a.CACHE_SIZE), NOW) == a.CACHE_SIZE


# --- The viewer's zone (decision 335) ----------------------------------------


def zoned(tz: str, first: str, last: str) -> a.Period:
    return a.resolve_period(TODAY, preset=None, first=first, last=last, tz=a.resolve_tz(tz))


def hours(window: Window) -> float:
    return (window.until - window.since) / dt.timedelta(hours=1)


def test_no_zone_asked_is_utc_and_reads_as_before() -> None:
    assert a.resolve_tz(None) == "UTC"
    got = period(**{"from": "2026-11-03", "to": "2026-11-03"})
    assert got.tz == "UTC"
    assert got.window == zoned("UTC", "2026-11-03", "2026-11-03").window


@pytest.mark.parametrize(
    ("tz", "since", "until"),
    [
        # UTC+1 all year: the day starts an hour before UTC's.
        ("Africa/Lagos", dt.datetime(2026, 11, 2, 23), dt.datetime(2026, 11, 3, 23)),
        # UTC-5 in November: five hours after.
        ("America/New_York", dt.datetime(2026, 11, 3, 5), dt.datetime(2026, 11, 4, 5)),
    ],
)
def test_a_day_runs_from_local_midnight_to_local_midnight(
    tz: str, since: dt.datetime, until: dt.datetime
) -> None:
    got = zoned(tz, "2026-11-03", "2026-11-03")
    assert got.tz == tz
    assert got.window == Window(
        since=since.replace(tzinfo=dt.UTC), until=until.replace(tzinfo=dt.UTC)
    )


@pytest.mark.parametrize(("day", "length"), [("2026-03-29", 23), ("2026-10-25", 25)])
def test_a_daylight_saving_day_is_23_or_25_hours(day: str, length: int) -> None:
    window = zoned("Europe/London", day, day).window
    assert hours(window) == length
    assert window.since.astimezone(ZoneInfo("Europe/London")).time() == dt.time(0)
    assert window.until.astimezone(ZoneInfo("Europe/London")).time() == dt.time(0)


def test_daily_buckets_across_a_clock_change_start_at_each_local_midnight() -> None:
    got = zoned("Europe/London", "2026-03-27", "2026-03-31")
    buckets = a.series_buckets(got)
    assert [hours(b) for b in buckets] == [24, 24, 23, 24, 24]
    assert [got.day_of(b.since) for b in buckets] == [
        dt.date(2026, 3, 27) + dt.timedelta(days=i) for i in range(5)
    ]
    assert buckets[0].since == got.window.since
    assert buckets[-1].until == got.window.until


def test_weekly_buckets_and_the_previous_period_keep_the_zone() -> None:
    got = zoned("Europe/London", "2026-10-20", "2026-11-03")
    weeks = a.series_buckets(got)
    # The first week holds the 25-hour day; each edge is a London midnight.
    assert [hours(w) for w in weeks] == [7 * 24 + 1, 7 * 24, 24]
    assert [got.day_of(w.since) for w in weeks] == [
        dt.date(2026, 10, 20),
        dt.date(2026, 10, 27),
        dt.date(2026, 11, 3),
    ]
    before = got.previous()
    assert before.tz == "Europe/London"
    assert before.window.until == got.window.since
    assert len(a.series_buckets(before)) == len(weeks)


def test_today_is_the_date_in_the_zone() -> None:
    late = dt.datetime(2026, 11, 10, 23, 30, tzinfo=dt.UTC)
    assert a.Period(first=TODAY, last=TODAY, tz="Africa/Lagos").day_of(late) == dt.date(
        2026, 11, 11
    )
    assert a.Period(first=TODAY, last=TODAY).day_of(late) == dt.date(2026, 11, 10)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "Mars/Olympus",
        "utc+1",
        "../etc/passwd",
        "Africa/",
        # The filesystem's own refusals (too long a name, a wildcard), which once
        # escaped as an OSError and a 500 (security review, 2026-11-11).
        "a" * 300,
        "Africa/" + "a" * 300,
        "a*b",
        "<script>",
        r"Africa\Lagos",
        # Accepted on a case-insensitive filesystem, refused on Linux: refused everywhere.
        "utc",
        "africa/lagos",
    ],
)
def test_a_zone_that_is_not_iana_is_refused_on_tz(bad: str) -> None:
    with pytest.raises(AnalyticsWindowError) as refused:
        a.resolve_tz(bad)
    assert refused.value.field_errors == (
        ("/query/tz", "must be an IANA time zone, e.g. Africa/Lagos"),
    )


@pytest.mark.parametrize(
    "good", ["UTC", "Africa/Lagos", "America/Argentina/Buenos_Aires", "Etc/GMT+5", "EST5EDT"]
)
def test_every_iana_shape_is_accepted(good: str) -> None:
    assert a.resolve_tz(good) == good


def test_a_zone_is_trimmed() -> None:
    assert a.resolve_tz(" Africa/Lagos ") == "Africa/Lagos"
