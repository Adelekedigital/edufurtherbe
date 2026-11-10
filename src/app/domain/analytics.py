"""The admin Analytics page's rules (PRD 31, decision 324): windows, buckets,
rates, the credit source groups and the alert copy.

Pure: no I/O. The stores in `infra/db/analytics_*.py` count; this module says
what a window is, how a count becomes a rate, and what an alert reads.

**A window is whole UTC days, inclusive** (PRD 31, *Window rules*): the presets
end today, a custom range is the admin's `from` and `to`, and the previous
period is the same number of days ending the day before. As instants it is the
half-open ``[first 00:00, last + 1 00:00)``, so `predicates.during` reads it
like every other dashboard window.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise
from types import MappingProxyType
from typing import Literal, get_args

from app.core.errors import AnalyticsWindowError
from app.domain.enums import CreditSource
from app.domain.mentor_stats import Window

#: The presets the window control offers, in days ending today.
PRESETS: Mapping[str, int] = MappingProxyType({"7d": 7, "30d": 30, "90d": 90})
#: What the page shows when nothing is asked.
DEFAULT_PRESET = "30d"
#: The first day a custom range may start (PRD 31: the picker's `min`).
EARLIEST_DAY = dt.date(2024, 6, 1)
#: A window this long or shorter draws its North Star by day, else by 7 days.
DAILY_UP_TO_DAYS = 14
#: Points in a feature card's sparkline: the window in this many equal parts.
SPARK_POINTS = 8

SeriesUnit = Literal["day", "week"]


@dataclass(frozen=True, slots=True)
class Period:
    """Whole UTC days, ``first`` to ``last`` inclusive."""

    first: dt.date
    last: dt.date

    @property
    def days(self) -> int:
        return (self.last - self.first).days + 1

    @property
    def window(self) -> Window:
        """The period as instants: ``[first 00:00 UTC, last + 1 day 00:00 UTC)``."""
        return Window(
            since=_midnight(self.first), until=_midnight(self.last) + dt.timedelta(days=1)
        )

    def previous(self) -> Period:
        """Same number of days, ending the day before this one starts."""
        last = self.first - dt.timedelta(days=1)
        return Period(first=last - dt.timedelta(days=self.days - 1), last=last)


def _midnight(day: dt.date) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, tzinfo=dt.UTC)


def _refuse(pointer: str, message: str) -> AnalyticsWindowError:
    return AnalyticsWindowError(message, field_errors=((pointer, message),))


def _day(value: str, pointer: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise _refuse(pointer, "must be a date, YYYY-MM-DD") from None


def resolve_period(
    today: dt.date, *, preset: str | None, first: str | None, last: str | None
) -> Period:
    """The period a request asks for, or `AnalyticsWindowError` naming why not.

    A preset **or** a range, never both; a range needs both ends (one day sends
    the same date twice); ``from`` no earlier than `EARLIEST_DAY`, ``to`` no
    later than ``today``, and ``from`` not after ``to``. Nothing asked is the
    default preset.
    """
    if preset is not None and (first is not None or last is not None):
        raise _refuse("/query/window", "send a window or from and to, not both")
    if first is None and last is None:
        days = PRESETS.get(preset if preset is not None else DEFAULT_PRESET)
        if days is None:
            raise _refuse("/query/window", "must be one of " + ", ".join(PRESETS))
        return Period(first=today - dt.timedelta(days=days - 1), last=today)
    if first is None or last is None:
        missing = "/query/from" if first is None else "/query/to"
        raise _refuse(missing, "a range needs both from and to")
    start, end = _day(first, "/query/from"), _day(last, "/query/to")
    if start < EARLIEST_DAY:
        raise _refuse("/query/from", f"must be {EARLIEST_DAY.isoformat()} or later")
    if end > today:
        raise _refuse("/query/to", "must be today or earlier")
    if start > end:
        raise _refuse("/query/from", "must not be after to")
    return Period(first=start, last=end)


def resolve_compare(value: str | None) -> bool:
    """``compare``: ``true`` (the default) or ``false``, nothing else."""
    if value is None or value == "true":
        return True
    if value == "false":
        return False
    raise _refuse("/query/compare", "must be true or false")


def series_unit(period: Period) -> SeriesUnit:
    return "day" if period.days <= DAILY_UP_TO_DAYS else "week"


def series_buckets(period: Period) -> list[Window]:
    """The North Star's points: one per day, or one per 7 days counted from
    the window's first day (the last may be shorter), so the current and the
    previous period match point by point. Not ISO weeks, by design."""
    step = 1 if series_unit(period) == "day" else 7
    window = period.window
    buckets: list[Window] = []
    since = window.since
    while since < window.until:
        until = min(since + dt.timedelta(days=step), window.until)
        buckets.append(Window(since=since, until=until))
        since = until
    return buckets


def spark_buckets(window: Window) -> list[Window]:
    """The window in `SPARK_POINTS` equal parts, oldest first."""
    part = (window.until - window.since) / SPARK_POINTS
    edges = [window.since + part * i for i in range(SPARK_POINTS)] + [window.until]
    return [Window(since=a, until=b) for a, b in pairwise(edges)]


def rate(numerator: int, denominator: int) -> float | None:
    """``numerator / denominator`` as a percentage, one decimal, halves away
    from zero (decision 171's rounding); ``None`` with nothing to divide by."""
    if denominator <= 0:
        return None
    percent = Decimal(numerator) * 100 / Decimal(denominator)
    return float(percent.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def ratio(numerator: int, denominator: int, places: int = 2) -> float | None:
    """A plain ratio rounded half up to ``places``; ``None`` over zero."""
    if denominator <= 0:
        return None
    value = Decimal(numerator) / Decimal(denominator)
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


# --- Credits -----------------------------------------------------------------

#: The Credits tab's rows. Since decision 308 a starter or a qualifying invite
#: is a level rise (`level_up`), and nothing records which caused one.
CreditGroup = Literal["monthly", "level_up", "admin", "refund", "carried_over"]
CREDIT_GROUPS: tuple[CreditGroup, ...] = get_args(CreditGroup)

#: Every lot source in exactly one row; total over `CreditSource` by test.
SOURCE_GROUP: Mapping[CreditSource, CreditGroup] = MappingProxyType(
    {
        CreditSource.MONTHLY_FREE: "monthly",
        CreditSource.LEVEL_TOP_UP: "level_up",
        CreditSource.ADMIN_GRANT: "admin",
        CreditSource.REFUND: "refund",
        CreditSource.OPENING_BALANCE: "carried_over",
        CreditSource.PROFILE_COMPLETED: "carried_over",
        CreditSource.REFERRAL_UNLOCK: "carried_over",
    }
)


@dataclass(slots=True)
class CreditFigures:
    """One row of the Credits tab, or its totals."""

    granted: int = 0
    spent: int = 0
    refunded: int = 0
    expired: int = 0


@dataclass(frozen=True, slots=True)
class CreditsTab:
    flow: CreditFigures
    by_source: dict[CreditGroup, CreditFigures]


# --- Alerts ------------------------------------------------------------------

AlertKey = Literal["login_code_failed", "no_booking_requests", "no_signups", "delivery_failed"]
AnalyticsTab = Literal["overview", "sessions", "supply", "credits", "growth", "funnels"]

#: Quiet for this long is an alert, for booking requests and for sign-ups.
QUIET_FOR = dt.timedelta(hours=24)
#: Outbox rows failed in one UTC day above this count raise `delivery_failed`.
DELIVERY_FAILED_OVER = 20


@dataclass(frozen=True, slots=True)
class AlertLink:
    tab: AnalyticsTab
    funnel: str | None = None
    by: str | None = None


@dataclass(frozen=True, slots=True)
class Alert:
    key: AlertKey
    title: str
    body: str
    link: AlertLink | None
    raised_at: dt.datetime


def quiet_alert(
    key: Literal["no_booking_requests", "no_signups"],
    last: dt.datetime | None,
    now: dt.datetime,
) -> Alert | None:
    """An alert when nothing happened in the last `QUIET_FOR`, raised when the
    quiet began (the last event plus `QUIET_FOR`, or now when there was none)."""
    if last is not None and now - last < QUIET_FOR:
        return None
    raised_at = now if last is None else last + QUIET_FOR
    if key == "no_booking_requests":
        return Alert(
            key=key,
            title="No booking requests",
            body="No booking requests in the last 24 hours.",
            link=AlertLink(tab="sessions"),
            raised_at=raised_at,
        )
    return Alert(
        key=key,
        title="No sign-ups",
        body="No one has signed up in the last 24 hours.",
        link=AlertLink(tab="funnels", funnel="signup"),
        raised_at=raised_at,
    )


def delivery_alert(failed_yesterday: int, today: dt.date) -> Alert | None:
    """An alert when more than `DELIVERY_FAILED_OVER` outbox rows failed
    yesterday (UTC), raised at the start of today. No link: the PRD hides the
    delivery card for now."""
    if failed_yesterday <= DELIVERY_FAILED_OVER:
        return None
    return Alert(
        key="delivery_failed",
        title="Messages not going out",
        body=f"{failed_yesterday} emails or analytics events failed yesterday.",
        link=None,
        raised_at=_midnight(today),
    )


def newest_first(alerts: list[Alert | None]) -> list[Alert]:
    """The alerts that fired, newest first (PRD 31: one banner each)."""
    fired = [alert for alert in alerts if alert is not None]
    return sorted(fired, key=lambda alert: alert.raised_at, reverse=True)
