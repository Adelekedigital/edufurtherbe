"""The admin Analytics page's rules (PRD 31, decision 324): windows, buckets,
rates, the credit source groups, which places the Supply tab shows, and the
alert copy.

Pure: no I/O. The stores in `infra/db/analytics_*.py` count; this module says
what a window is, how a count becomes a rate, and what an alert reads.

**A window is whole days in the viewer's zone, inclusive** (PRD 31, *Window
rules*; decision 335): the presets end today there, a custom range is the
admin's `from` and `to`, and the previous period is the same number of days
ending the day before. As instants it is the half-open ``[first 00:00,
last + 1 00:00)`` at local midnights (`local_day_start`, so a DST day is 23 or
25 hours), and `predicates.during` reads it like every other dashboard window.
No zone asked is `UTC`.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from itertools import pairwise
from types import MappingProxyType
from typing import Literal, get_args

from app.core.errors import AnalyticsWindowError
from app.domain.accounts import UNLISTED_ON_DELETION
from app.domain.activity import activity_day
from app.domain.availability import (
    UnknownTimezoneError,
    local_date,
    local_day_start,
    normalise_timezone,
)
from app.domain.enums import (
    ApplicationStage,
    CreditSource,
    MissedReason,
    PrimaryRole,
    UnlistedReason,
)
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
#: The zone a request without `tz` reads its days in.
DEFAULT_TZ = "UTC"
#: The zone the platform-wide alerts read "yesterday" in, whoever is looking:
#: one banner for every admin (decision 335). Its own name, so changing
#: `DEFAULT_TZ` can never move the alerts.
PLATFORM_TZ = "UTC"
#: How a custom range's dates must be written.
_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")

SeriesUnit = Literal["day", "week"]


@dataclass(frozen=True, slots=True)
class Period:
    """Whole days in ``tz`` (an IANA name), ``first`` to ``last`` inclusive."""

    first: dt.date
    last: dt.date
    tz: str = DEFAULT_TZ

    @property
    def days(self) -> int:
        return (self.last - self.first).days + 1

    @property
    def window(self) -> Window:
        """The period as instants: ``[first 00:00, last + 1 day 00:00)`` in ``tz``."""
        return self.between(self.first, self.last + dt.timedelta(days=1))

    def between(self, first: dt.date, after: dt.date) -> Window:
        """``[first 00:00, after 00:00)`` in ``tz``, as instants."""
        return Window(since=local_day_start(first, self.tz), until=local_day_start(after, self.tz))

    def previous(self) -> Period:
        """Same number of days, ending the day before this one starts."""
        return self.ending(self.first - dt.timedelta(days=1), self.days)

    def ending(self, last: dt.date, days: int) -> Period:
        """``days`` whole days ending with ``last``, in the same zone."""
        return Period(first=last - dt.timedelta(days=days - 1), last=last, tz=self.tz)

    def day_of(self, moment: dt.datetime) -> dt.date:
        """The date ``moment`` falls on in ``tz``."""
        return local_date(self.tz, moment)


def _refuse(pointer: str, message: str) -> AnalyticsWindowError:
    return AnalyticsWindowError(message, field_errors=((pointer, message),))


def _day(value: str, pointer: str) -> dt.date:
    """A date written `YYYY-MM-DD` and nothing else (`fromisoformat` alone also
    reads `20260101` and ISO week dates)."""
    try:
        if not _ISO_DAY.fullmatch(value):
            raise ValueError(value)
        return dt.date.fromisoformat(value)
    except ValueError:
        raise _refuse(pointer, "must be a date, YYYY-MM-DD") from None


def resolve_tz(value: str | None) -> str:
    """``tz``: an IANA zone name (`normalise_timezone`, trimmed), `DEFAULT_TZ`
    when absent, else `AnalyticsWindowError` on ``/query/tz``."""
    if value is None:
        return DEFAULT_TZ
    try:
        return normalise_timezone(value)
    except UnknownTimezoneError:
        raise _refuse("/query/tz", "must be an IANA time zone, e.g. Africa/Lagos") from None


def resolve_period(
    today: dt.date,
    *,
    preset: str | None,
    first: str | None,
    last: str | None,
    tz: str = DEFAULT_TZ,
) -> Period:
    """The period a request asks for, in ``tz`` (resolved, with ``today`` the
    date there), or `AnalyticsWindowError` naming why not.

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
        return Period(first=today - dt.timedelta(days=days - 1), last=today, tz=tz)
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
    return Period(first=start, last=end, tz=tz)


def resolve_compare(value: str | None) -> bool:
    """``compare``: ``true`` (the default) or ``false``, nothing else."""
    if value is None or value == "true":
        return True
    if value == "false":
        return False
    raise _refuse("/query/compare", "must be true or false")


def series_unit(period: Period) -> SeriesUnit:
    return "day" if period.days <= DAILY_UP_TO_DAYS else "week"


def _bucket_days(period: Period) -> list[tuple[dt.date, dt.date]]:
    """Each North Star bucket's first day and the day after its last."""
    every = 1 if series_unit(period) == "day" else 7
    after = period.last + dt.timedelta(days=1)
    starts = [period.first + dt.timedelta(days=d) for d in range(0, period.days, every)]
    return list(zip(starts, [*starts[1:], after], strict=True))


def series_buckets(period: Period) -> list[Window]:
    """The North Star's points: one per day, or one per 7 days counted from
    the window's first day (the last may be shorter), so the current and the
    previous period match point by point. Not ISO weeks, by design. Each edge
    is a local midnight in the period's zone."""
    return [period.between(a, b) for a, b in _bucket_days(period)]


def spark_buckets(window: Window) -> list[Window]:
    """The window in `SPARK_POINTS` equal parts, oldest first."""
    part = (window.until - window.since) / SPARK_POINTS
    edges = [window.since + part * i for i in range(SPARK_POINTS)] + [window.until]
    return [Window(since=a, until=b) for a, b in pairwise(edges)]


def rounded(value: object, places: int = 1) -> float | None:
    """``value`` (a `Decimal`, or a number from the database) to ``places``
    decimals, halves away from zero (decision 171's rounding): the one rounding
    every analytics figure goes through. ``None`` stays ``None``."""
    if value is None:
        return None
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def rate(numerator: int, denominator: int) -> float | None:
    """``numerator / denominator`` as a percentage, one decimal, halves away
    from zero; ``None`` with nothing to divide by."""
    if denominator <= 0:
        return None
    return rounded(Decimal(numerator) * 100 / Decimal(denominator))


def ratio(numerator: int, denominator: int, places: int = 2) -> float | None:
    """A plain ratio rounded half up to ``places``; ``None`` over zero."""
    if denominator <= 0:
        return None
    return rounded(Decimal(numerator) / Decimal(denominator), places)


def days_of(seconds: float | None) -> float | None:
    """A duration in seconds as days, one decimal, half up; ``None`` stays."""
    if seconds is None:
        return None
    return rounded(Decimal(str(seconds)) / 86400)


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


# --- Supply ------------------------------------------------------------------

#: Unlistings that are a side effect of something else, so not counted as one:
#: a decline (`decide` writes `never_approved`) and a deleted account.
SIDE_EFFECT_UNLISTINGS: tuple[str, ...] = (
    UnlistedReason.NEVER_APPROVED.value,
    UNLISTED_ON_DELETION,
)
#: The most places a by-country or by-school list shows.
TOP_PLACES = 5


@dataclass(frozen=True, slots=True)
class SupplyCohort:
    """Applications submitted in the window, by where they stand now."""

    applied: int
    approved: int
    listed: int
    bookable: int


@dataclass(frozen=True, slots=True)
class SupplyPlace:
    """A country or school mentees aim for, and the mentors who can take them."""

    key: str
    name: str
    mentees: int
    bookable_mentors: int


@dataclass(frozen=True, slots=True)
class SupplyTab:
    cohort: SupplyCohort
    median_review_days: float | None
    unlisted: int
    by_country: list[SupplyPlace]
    by_school: list[SupplyPlace]


def neediest(places: list[SupplyPlace]) -> list[SupplyPlace]:
    """The `TOP_PLACES` places mentees are least served in: no bookable mentor
    first, then most mentees per mentor, then by name. With no mentor the
    ratio is unbounded, so those places tie and read by name."""

    def need(place: SupplyPlace) -> tuple[bool, Fraction, str]:
        served = place.bookable_mentors > 0
        per_mentor = Fraction(place.mentees, place.bookable_mentors) if served else Fraction(0)
        return served, -per_mentor, place.name

    return sorted(places, key=need)[:TOP_PLACES]


# --- Alerts ------------------------------------------------------------------

AlertKey = Literal["login_code_failed", "no_booking_requests", "no_signups", "delivery_failed"]
AnalyticsTab = Literal["overview", "sessions", "supply", "credits", "growth", "funnels"]

#: Quiet for this long is an alert, for booking requests and for sign-ups
#: (owner, 2026-11-12: a week; a day was too short).
QUIET_FOR = dt.timedelta(days=7)
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
            body=f"No one has asked for a session in the last {QUIET_FOR.days} days.",
            link=AlertLink(tab="sessions"),
            raised_at=raised_at,
        )
    return Alert(
        key=key,
        title="No sign-ups",
        body=f"No one has signed up in the last {QUIET_FOR.days} days.",
        link=AlertLink(tab="funnels", funnel="signup"),
        raised_at=raised_at,
    )


def yesterday(today: dt.date) -> Period:
    """The `PLATFORM_TZ` day before ``today``: the delivery alert's window."""
    day = today - dt.timedelta(days=1)
    return Period(first=day, last=day, tz=PLATFORM_TZ)


def delivery_alert(failed_yesterday: int, today: dt.date) -> Alert | None:
    """An alert when more than `DELIVERY_FAILED_OVER` outbox rows failed
    yesterday (UTC), raised at the start of today. No link: the PRD hides the
    delivery card for now."""
    if failed_yesterday <= DELIVERY_FAILED_OVER:
        return None
    return Alert(
        key="delivery_failed",
        title="Messages not going out",
        body=f"{failed_yesterday} emails, notifications or analytics events failed yesterday.",
        link=None,
        raised_at=local_day_start(today, PLATFORM_TZ),
    )


def newest_first(alerts: list[Alert | None]) -> list[Alert]:
    """The alerts that fired, newest first (PRD 31: one banner each)."""
    fired = [alert for alert in alerts if alert is not None]
    return sorted(fired, key=lambda alert: alert.raised_at, reverse=True)


# --- Overview and Sessions ---------------------------------------------------

Number = int | float

#: The seven feature cards, always all, in this order (PRD 31).
FeatureKey = Literal[
    "booking",
    "explore",
    "messaging",
    "mentor_supply",
    "invites",
    "mentee_activation",
    "mentor_activation",
]
FEATURES: tuple[FeatureKey, ...] = get_args(FeatureKey)


@dataclass(frozen=True, slots=True)
class Metric:
    """A figure and the same measure over the previous period, or ``None``
    when nothing is compared or the figure exists only now."""

    value: Number | None
    previous: Number | None


def compared(value: Number | None, previous: Number | None, compare: bool) -> Metric:
    return Metric(value=value, previous=previous if compare else None)


@dataclass(frozen=True, slots=True)
class SeriesPoint:
    start: dt.date
    value: int
    previous: int | None


@dataclass(frozen=True, slots=True)
class NorthStar:
    completed: Metric
    unit: SeriesUnit
    series: list[SeriesPoint]


@dataclass(frozen=True, slots=True)
class SubTracks:
    unique_mentees: Metric
    mentors_delivered: Metric
    sessions_per_mentee: Metric
    weekly_active_users: Metric
    monthly_active_users: Metric
    stickiness: Metric


#: Active users are people with a recorded activity day (decision 342) in this
#: many days ending with the window's last day.
WEEKLY_ACTIVE_DAYS = 7
MONTHLY_ACTIVE_DAYS = 30


def active_spans(period: Period, days: int) -> tuple[Period, Period]:
    """``days`` whole days ending with the window's last day, and the same
    ending with the previous period's, in the window's zone."""
    return period.ending(period.last, days), period.ending(period.previous().last, days)


def activity_days(span: Period) -> tuple[dt.date, dt.date]:
    """The first and last activity day (a UTC date) that overlaps ``span``'s
    local days: one never missed inside it, and at most one partial day
    outside it, at whichever end the zone's offset falls (decision 342)."""
    window = span.window
    return activity_day(window.since), activity_day(window.until - dt.timedelta(microseconds=1))


@dataclass(frozen=True, slots=True)
class ActiveUsers:
    """Distinct counted people active in the last 7 and the last 30 days."""

    weekly: int
    monthly: int

    @property
    def stickiness(self) -> float | None:
        """Weekly over monthly, a percentage with one decimal; ``None`` with
        nobody active in the month."""
        return rate(self.weekly, self.monthly)


@dataclass(frozen=True, slots=True)
class ActiveMetrics:
    """Weekly, monthly and stickiness, each against the previous period."""

    weekly: Metric
    monthly: Metric
    stickiness: Metric


def active_metrics(now: ActiveUsers, before: ActiveUsers, compare: bool) -> ActiveMetrics:
    return ActiveMetrics(
        weekly=compared(now.weekly, before.weekly, compare),
        monthly=compared(now.monthly, before.monthly, compare),
        stickiness=compared(now.stickiness, before.stickiness, compare),
    )


def active_tracks(now: ActiveUsers, before: ActiveUsers, compare: bool) -> dict[str, Metric]:
    """The three active-user sub-tracks, by their wire names."""
    figures = active_metrics(now, before, compare)
    return {
        "weekly_active_users": figures.weekly,
        "monthly_active_users": figures.monthly,
        "stickiness": figures.stickiness,
    }


def bucket_weeks(period: Period) -> list[tuple[dt.date, Period]]:
    """Each North Star bucket's first day, and the `WEEKLY_ACTIVE_DAYS` days
    ending with its last day: the span the weekly-active trend reads there
    (decision 343)."""
    return [
        (first, period.ending(after - dt.timedelta(days=1), WEEKLY_ACTIVE_DAYS))
        for first, after in _bucket_days(period)
    ]


#: A count per trend point, for each primary role.
RoleCounts = Mapping[PrimaryRole, list[int]]


def everybody(by_role: Mapping[PrimaryRole, ActiveUsers]) -> ActiveUsers:
    """Everybody active: the roles added up. Exact because `primary_role` is
    NOT NULL and has only these members, so each person is in one role."""
    return ActiveUsers(
        weekly=sum(users.weekly for users in by_role.values()),
        monthly=sum(users.monthly for users in by_role.values()),
    )


@dataclass(frozen=True, slots=True)
class WeeklyActive:
    """Weekly actives at one trend point and its previous-period match; null
    where the span was not fully recorded (and ``previous`` without compare)."""

    value: int | None
    previous: int | None


@dataclass(frozen=True, slots=True)
class ActivePoint:
    """One point of the weekly-active trend, in total and by primary role."""

    start: dt.date
    total: WeeklyActive
    by_role: dict[PrimaryRole, WeeklyActive]


def history_from(earliest: dt.date | None) -> dt.date | None:
    """The first full day of recorded activity: the day after ``earliest``,
    the first day recorded, which recording began part-way through. ``None``
    with nothing recorded."""
    return None if earliest is None else earliest + dt.timedelta(days=1)


def _recorded(span: Period, earliest: dt.date | None) -> bool:
    """Every day of ``span`` was recorded in full: its first activity day (a
    UTC date, `activity_days`) is on or after `history_from`. Nothing
    recorded is never."""
    first_full = history_from(earliest)
    return first_full is not None and activity_days(span)[0] >= first_full


def active_series(
    period: Period,
    now: RoleCounts,
    before: RoleCounts,
    earliest: dt.date | None,
    compare: bool,
) -> list[ActivePoint]:
    """The weekly-active trend on the North Star's points: ``now`` and
    ``before`` count each `bucket_weeks` span of the period and of the
    previous one, point by point, per role (``before`` unread without
    compare); ``earliest`` is the first activity day. The total is the roles
    added up (`everybody`); a role is null wherever its point is."""

    def weekly(
        roles: tuple[PrimaryRole, ...], i: int, shown: bool, before_shown: bool
    ) -> WeeklyActive:
        return WeeklyActive(
            value=sum(now[role][i] for role in roles) if shown else None,
            previous=sum(before[role][i] for role in roles) if before_shown else None,
        )

    points = []
    for i, ((start, span), (_, span_before)) in enumerate(
        zip(bucket_weeks(period), bucket_weeks(period.previous()), strict=True)
    ):
        seen = (_recorded(span, earliest), compare and _recorded(span_before, earliest))
        points.append(
            ActivePoint(
                start=start,
                total=weekly(tuple(PrimaryRole), i, *seen),
                by_role={role: weekly((role,), i, *seen) for role in PrimaryRole},
            )
        )
    return points


@dataclass(frozen=True, slots=True)
class ActiveMembers:
    """The Overview's active members (decision 343): the sub-tracks' three
    figures for each primary role, and the weekly-active trend."""

    by_role: dict[PrimaryRole, ActiveMetrics]
    series: list[ActivePoint]
    #: `history_from`: the day the series' null rule starts counting from.
    history_from: dt.date | None


@dataclass(frozen=True, slots=True)
class Feature:
    key: FeatureKey
    value: Metric | None
    spark: list[float | None] | None
    contribution: Number | None


@dataclass(frozen=True, slots=True)
class OverviewTab:
    north_star: NorthStar
    sub_tracks: SubTracks
    features: list[Feature]
    active_members: ActiveMembers


@dataclass(frozen=True, slots=True)
class TopReason:
    reason: MissedReason
    share: float


@dataclass(frozen=True, slots=True)
class SessionsTab:
    kpis: dict[str, Metric]
    outcomes: dict[str, int]
    top_no_show_reason: TopReason | None
    guardrails: dict[str, Metric]


# --- Growth ------------------------------------------------------------------

#: Where a sign-up came from, and through which channel: empty lists until
#: PRD 30 records `signup_source`.
SignupSource = Literal["invite", "direct", "campaign", "referral_site"]
SignupChannel = Literal["linkedin", "whatsapp", "instagram", "google", "newsletter", "other"]
#: The Growth tab's month series: this many UTC months, ending with this one.
GROWTH_MONTHS = 6
#: A session whose type names no stage, or that has no type.
StageKey = ApplicationStage | Literal["not_set"]
NOT_SET: Literal["not_set"] = "not_set"


@dataclass(frozen=True, slots=True)
class SignUps:
    everyone: Metric
    mentees: Metric
    mentors: Metric


@dataclass(frozen=True, slots=True)
class TopInviter:
    name: str
    joined: int


@dataclass(frozen=True, slots=True)
class InviteCohort:
    """Invites made in the window, and how far their invitees got."""

    invited: int
    joined: int
    qualified: int
    first_booking: int
    first_completed: int
    top_inviter: TopInviter | None


@dataclass(frozen=True, slots=True)
class MonthCount:
    month: str
    count: int


@dataclass(frozen=True, slots=True)
class Migrated:
    count: int
    on: dt.date | None


@dataclass(frozen=True, slots=True)
class TopicCount:
    key: str
    name: str
    count: int


@dataclass(frozen=True, slots=True)
class StageCount:
    key: StageKey
    count: int


@dataclass(frozen=True, slots=True)
class GrowthTab:
    signups: SignUps
    invites: InviteCohort
    months: list[MonthCount]
    migrated: Migrated | None
    completed_total: int
    by_topic: list[TopicCount]
    by_stage: list[StageCount]


# --- Cache -------------------------------------------------------------------

#: How long a tab's database figures may be served again (PRD 31: "cached for
#: up to 10 minutes, keyed on the query"). Alerts are never cached.
CACHE_FOR = dt.timedelta(minutes=10)
#: Entries kept; the oldest goes first past it, so memory stays bounded.
CACHE_SIZE = 64


class TabCache:
    """A tab's figures by (tab, first day, last day, zone, compare), each kept until
    `CACHE_FOR` after it was computed. In-process and per app, so a figure is
    at most `CACHE_FOR` old and each instance warms its own."""

    def __init__(self) -> None:
        self._entries: dict[tuple[object, ...], tuple[dt.datetime, object]] = {}

    def get(self, key: tuple[object, ...], now: dt.datetime) -> object | None:
        entry = self._entries.get(key)
        if entry is None or entry[0] <= now:
            self._entries.pop(key, None)
            return None
        return entry[1]

    def put(
        self,
        key: tuple[object, ...],
        value: object,
        now: dt.datetime,
        keep_for: dt.timedelta = CACHE_FOR,
    ) -> None:
        self._entries.pop(key, None)
        while len(self._entries) >= CACHE_SIZE:
            self._entries.pop(next(iter(self._entries)))
        self._entries[key] = (now + keep_for, value)
