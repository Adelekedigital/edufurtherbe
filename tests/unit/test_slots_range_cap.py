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

from app.domain.availability import BookingWindow

#: The westernmost and easternmost zones: UTC-12 and UTC+14.
WEST, EAST = ZoneInfo("Etc/GMT+12"), ZoneInfo("Pacific/Kiritimati")


def span_needed(now: dt.datetime, viewer: ZoneInfo, mentor: ZoneInfo, max_days: int) -> int:
    """Days from the viewer's margin day to the day after the mentor's last window day."""
    viewer_start = now.astimezone(viewer).date() - dt.timedelta(days=1)
    mentor_last = (now + dt.timedelta(days=max_days)).astimezone(mentor).date()
    return (mentor_last + dt.timedelta(days=1) - viewer_start).days


@pytest.mark.parametrize("max_days", [4, 14, 56])
@pytest.mark.parametrize("hour", range(24))
def test_the_cap_covers_the_furthest_apart_zones(max_days: int, hour: int) -> None:
    """Codex's case is 11:00 UTC with 56 days: 60 days needed. Every hour is
    checked, both ways round, because which pair is worst depends on the hour."""
    window = BookingWindow(max_days=max_days, default_days=max_days)
    now = dt.datetime(2026, 9, 30, hour, tzinfo=dt.UTC)

    worst = max(span_needed(now, WEST, EAST, max_days), span_needed(now, EAST, WEST, max_days))

    assert worst <= window.range_cap_days


def test_codexs_example_needs_sixty_days() -> None:
    now = dt.datetime(2026, 9, 30, 11, tzinfo=dt.UTC)

    assert span_needed(now, WEST, EAST, 56) == 60


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
