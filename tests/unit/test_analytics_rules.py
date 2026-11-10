"""The admin Analytics rules (PRD 31, decision 324): windows, buckets, rates,
the credit source groups and the alert thresholds."""

from __future__ import annotations

import datetime as dt
import itertools

import pytest

from app.core.errors import AnalyticsWindowError
from app.domain import analytics as a
from app.domain.enums import CreditSource

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


def test_quiet_for_less_than_a_day_raises_nothing() -> None:
    last = NOW - a.QUIET_FOR + dt.timedelta(seconds=1)
    assert a.quiet_alert("no_booking_requests", last, NOW) is None


def test_quiet_for_a_day_raises_when_the_quiet_began() -> None:
    last = NOW - a.QUIET_FOR
    alert = a.quiet_alert("no_signups", last, NOW)
    assert alert is not None
    assert alert.raised_at == NOW
    assert alert.link == a.AlertLink(tab="funnels", funnel="signup")


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
    old = a.quiet_alert("no_signups", NOW - dt.timedelta(days=3), NOW)
    new = a.quiet_alert("no_booking_requests", NOW - dt.timedelta(days=1), NOW)
    assert [x.key for x in a.newest_first([old, None, new])] == [
        "no_booking_requests",
        "no_signups",
    ]
