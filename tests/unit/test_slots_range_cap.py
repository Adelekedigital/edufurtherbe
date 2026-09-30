"""The widest `/slots` range must cover the whole window from any pair of zones.

A client asks in the viewer's calendar dates; `/slots` reads them in the
mentor's. The two furthest-apart IANA zones are 26 hours apart, which can put
their local dates two days apart, so the cap is checked here against the worst
real pair rather than reasoned about.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from app.domain.availability import ZONE_DATE_GAP, BookingWindow

#: The westernmost and easternmost zones: UTC-12 and UTC+14.
WEST, EAST = ZoneInfo("Etc/GMT+12"), ZoneInfo("Pacific/Kiritimati")


def padded_request(now: dt.datetime, viewer: ZoneInfo, max_days: int) -> tuple[dt.date, dt.date]:
    """The range a client sends in its own dates: `ZONE_DATE_GAP` days of padding
    before its today and after the window's last date, `end` exclusive."""
    today = now.astimezone(viewer).date()
    start = today - dt.timedelta(days=ZONE_DATE_GAP)
    end = today + dt.timedelta(days=max_days + ZONE_DATE_GAP + 1)
    return start, end


@pytest.mark.parametrize("max_days", [4, 14, 56])
@pytest.mark.parametrize("hour", range(24))
@pytest.mark.parametrize(("viewer", "mentor"), [(WEST, EAST), (EAST, WEST)])
def test_a_padded_request_covers_the_whole_window_from_any_zone(
    max_days: int, hour: int, viewer: ZoneInfo, mentor: ZoneInfo
) -> None:
    """Both edges, both ways round, every hour: the request starts on or before
    the mentor's today, ends after the mentor's last window date, and fits the
    cap. Codex's cases are 11:00 UTC, west-to-east (trailing) and east-to-west
    (leading)."""
    window = BookingWindow(max_days=max_days, default_days=max_days)
    now = dt.datetime(2026, 9, 30, hour, tzinfo=dt.UTC)
    mentor_first = now.astimezone(mentor).date()
    mentor_last = (now + dt.timedelta(days=max_days)).astimezone(mentor).date()

    start, end = padded_request(now, viewer, max_days)

    assert start <= mentor_first
    assert end > mentor_last
    assert (end - start).days <= window.range_cap_days


def test_the_published_422_quotes_the_enforced_cap() -> None:
    """The spec's number comes from `range_cap_days`, not a retyped literal."""
    from app.core.config import Settings
    from app.main import create_app

    settings = Settings(_env_file=None)
    cap = BookingWindow(
        max_days=settings.max_booking_window_days,
        default_days=settings.default_booking_window_days,
    ).range_cap_days
    spec = create_app(settings).openapi()
    slots = next(ops["get"] for path, ops in spec["paths"].items() if path.endswith("/slots"))

    text = slots["responses"]["422"]["description"]

    assert f"+ {cap - settings.max_booking_window_days} days" in text
    assert f"{cap} by default" in text
