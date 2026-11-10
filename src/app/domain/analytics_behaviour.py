"""The admin Analytics page's product-analytics reads (PRD 31 PR 5, decision 331):
the five funnels, the seven event cards, the Explore card and the log-in alert.

Pure: no I/O, and the vendor is never named. A funnel is declared here as
events and property matches; `infra` turns it into the vendor's query. Every
event name and property value is PRD 30's catalogue verbatim, because those
names are what the frontend tracks.

**One funnel per journey, a breakdown is a filter** (PRD 30): ``by`` narrows
the people counted (a person property) or the step a journey starts from (an
event property), never builds a second funnel. Each funnel's ``by`` is a closed
set; anything else is refused before anything is read.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from types import MappingProxyType
from typing import Literal, get_args

from app.core.errors import AnalyticsWindowError, NotFoundError
from app.domain.analytics import (
    Alert,
    AlertLink,
    Feature,
    Metric,
    Period,
    compared,
    rate,
    rounded,
    spark_buckets,
)
from app.domain.analytics_events import AnalyticsEvent
from app.domain.enums import EntryPoint, PrimaryRole, SignupSource
from app.domain.mentor_stats import Window

__all__ = [
    "BOOKED_WITHIN",
    "EVERYONE",
    "FUNNELS",
    "FUNNEL_NAMES",
    "BehaviourFigures",
    "BehaviourTab",
    "Breakdown",
    "Counted",
    "ExploreFigures",
    "Funnel",
    "FunnelName",
    "FunnelStep",
    "FunnelTab",
    "LoginCodes",
    "Match",
    "Searchers",
    "Share",
    "Step",
    "behaviour_tab",
    "breakdown_for",
    "explore_feature",
    "explore_windows",
    "funnel_named",
    "funnel_tab",
    "login_alert",
]

FunnelName = Literal[
    "signup", "booking", "mentee_activation", "mentor_activation", "mentor_application"
]
FUNNEL_NAMES: tuple[FunnelName, ...] = get_args(FunnelName)

#: Every funnel offers everyone; this is that option's key.
EVERYONE = "all"


@dataclass(frozen=True, slots=True)
class Match:
    """One property condition: ``key`` equals ``value``, or with ``value``
    ``None``, ``key`` is set at all. ``on`` says whose property it is."""

    key: str
    value: str | None = None
    on: Literal["event", "person"] = "event"
    #: The condition fails instead: a missing property is not ``value``.
    negate: bool = False


@dataclass(frozen=True, slots=True)
class Step:
    """A funnel step: any of ``events`` (all of ``where`` holding). With no
    event, nothing tracks the step yet and its count is ``None``."""

    key: str
    label: str
    events: tuple[str, ...]
    where: tuple[Match, ...] = ()


@dataclass(frozen=True, slots=True)
class Breakdown:
    """What one ``by`` option narrows: the people counted (``step`` ``None``,
    person properties) or the first step's events."""

    matches: tuple[Match, ...] = ()
    step: int | None = None


@dataclass(frozen=True, slots=True)
class Funnel:
    name: FunnelName
    steps: tuple[Step, ...]
    #: How long a person has from the first step to the last.
    window_days: int
    note: str
    #: Who is counted at all, whatever ``by``.
    people: tuple[Match, ...] = ()
    by: Mapping[str, Breakdown] = field(
        default_factory=lambda: MappingProxyType({EVERYONE: Breakdown()})
    )

    @property
    def tracked(self) -> tuple[Step, ...]:
        """The steps the vendor is asked about: those with an event, which
        are always a leading run (an untracked step is only ever last)."""
        return tuple(step for step in self.steps if step.events)


# The event names, verbatim from PRD 30's catalogue.
SIGNUP_STARTED = "signup_started"
LOGIN_VERIFIED, LOGIN_FAILED = "login_code_verified", "login_code_failed"
CODE_VERIFIED = (LOGIN_VERIFIED, "google_oauth_completed")
ONBOARDING_COMPLETED = "onboarding_completed"
#: The server's own events, by the names it sends them under.
BOOKING_REQUESTED = AnalyticsEvent.BOOKING_REQUESTED.value
SESSION_COMPLETED = AnalyticsEvent.SESSION_COMPLETED.value
MENTOR_LISTED = AnalyticsEvent.MENTOR_LISTED.value
SEARCH = "mentor_search_performed"
PROFILE_VIEWED = "mentor_profile_viewed"
BOOKING_STARTED = "booking_started"
CONFERENCING_CONNECTED = "conferencing_connected"
JOIN_CLICKED, JOIN_FAILED = "session_join_clicked", "session_join_failed"
WRITING_ASSIST = "writing_assist_used"
APP_INSTALLED, PUSH_ALLOWED = "pwa_installed", "push_permission_granted"
REQUEST_FAILED = "request_failed"

#: The server's `booking_requested` carries the session; the client's does
#: not. A funnel's request step is the server's, so an ad blocker cannot drop it.
FROM_SERVER = Match("session_id")

_ROLE = {role.value: Breakdown((Match("role", role.value, on="person"),)) for role in PrimaryRole}


def _sources(*sources: SignupSource) -> dict[str, Breakdown]:
    return {s.value: Breakdown((Match("signup_source", s.value, on="person"),)) for s in sources}


def _everyone(options: Mapping[str, Breakdown]) -> Mapping[str, Breakdown]:
    return MappingProxyType({EVERYONE: Breakdown(), **options})


#: A new mentee: not a migrated account logging in again.
_NEW_MENTEE = (
    Match("role", PrimaryRole.MENTEE.value, on="person"),
    Match("is_migrated", "true", on="person", negate=True),
)

FUNNELS: Mapping[FunnelName, Funnel] = MappingProxyType(
    {
        "signup": Funnel(
            name="signup",
            steps=(
                Step("started", "Started sign-up", (SIGNUP_STARTED,)),
                Step("code_verified", "Code verified", CODE_VERIFIED),
                Step("onboarded", "Onboarding done", (ONBOARDING_COMPLETED,)),
                Step(
                    "first_booking_or_bookable",
                    "First booking or bookable",
                    (BOOKING_REQUESTED, CONFERENCING_CONNECTED),
                ),
                Step("first_completed", "First completed session", (SESSION_COMPLETED,)),
            ),
            # Long enough for a mentor's 30 days and a mentee's 14.
            window_days=30,
            note=(
                "One sign-up funnel for both roles; role and source are breakdowns, "
                "not separate funnels."
            ),
            by=_everyone(
                {
                    **_ROLE,
                    **_sources(
                        SignupSource.INVITE,
                        SignupSource.DIRECT,
                        SignupSource.CAMPAIGN,
                        SignupSource.MIGRATED,
                    ),
                }
            ),
        ),
        "booking": Funnel(
            name="booking",
            steps=(
                Step("started", "Booking started", (BOOKING_STARTED,)),
                *(
                    Step(key, label, ("booking_step_completed",), (Match("step", key),))
                    for key, label in (
                        ("type", "Type picked"),
                        ("time", "Time picked"),
                        ("questions", "Questions done"),
                    )
                ),
                Step("requested", "Requested", (BOOKING_REQUESTED,), (FROM_SERVER,)),
                Step("completed", "Completed", (SESSION_COMPLETED,)),
            ),
            # A request may be for a time up to the 56-day booking window ahead.
            window_days=60,
            note="Requested and Completed come from the server, so ad blockers can't drop them.",
            by=_everyone(
                {p.value: Breakdown((Match("entry_point", p.value),), step=0) for p in EntryPoint}
            ),
        ),
        "mentee_activation": Funnel(
            name="mentee_activation",
            steps=(
                Step("signed_up", "Signed up", (SIGNUP_STARTED,)),
                Step("onboarded", "Onboarding done", (ONBOARDING_COMPLETED,)),
                Step("first_request", "First request", (BOOKING_REQUESTED,), (FROM_SERVER,)),
                Step("first_session", "First session (14 days)", (SESSION_COMPLETED,)),
            ),
            window_days=14,
            note="Target window: 14 days from sign-up.",
            people=_NEW_MENTEE,
            by=_everyone(_sources(SignupSource.INVITE, SignupSource.DIRECT)),
        ),
        "mentor_activation": Funnel(
            name="mentor_activation",
            steps=(
                # An approval lists the mentor in the same transaction, so the
                # server's first `mentor_listed` is the approval.
                Step("approved", "Approved", (MENTOR_LISTED,)),
                Step("availability_set", "Availability set", ("availability_set",)),
                Step("session_type_created", "Session created", ("session_type_created",)),
                Step(
                    CONFERENCING_CONNECTED,
                    "Video connected (bookable)",
                    (CONFERENCING_CONNECTED,),
                ),
                # `session_completed` is the mentee's event: nothing tells
                # analytics a mentor delivered one yet.
                Step("first_session", "First session (30 days)", ()),
            ),
            window_days=30,
            note="Bookable target: 7 days from approval.",
        ),
        "mentor_application": Funnel(
            name="mentor_application",
            steps=(
                Step("started", "Started", ("mentor_application_started",)),
                *(
                    Step(key, label, ("mentor_application_step_completed",), (Match("step", key),))
                    for key, label in (
                        ("about_you", "About you"),
                        ("education", "Education"),
                        ("hours", "Hours"),
                    )
                ),
                Step("submitted", "Submitted", ("mentor_application_submitted",)),
                Step("approved", "Approved", (MENTOR_LISTED,)),
            ),
            window_days=30,
            note="Approved comes from the server.",
        ),
    }
)


def funnel_named(name: str) -> Funnel:
    """The funnel, or `NotFoundError`: an unknown name is no such resource."""
    for key, funnel in FUNNELS.items():
        if key == name:
            return funnel
    raise NotFoundError("no such funnel")


def breakdown_for(funnel: Funnel, by: str | None) -> str:
    """``by`` when the funnel offers it (nothing asked is everyone), else the
    window's `422`, naming the options."""
    asked = EVERYONE if by is None else by
    if asked not in funnel.by:
        message = "must be one of " + ", ".join(funnel.by)
        raise AnalyticsWindowError(message, field_errors=(("/query/by", message),))
    return asked


@dataclass(frozen=True, slots=True)
class FunnelStep:
    key: str
    label: str
    count: Metric | None


@dataclass(frozen=True, slots=True)
class FunnelTab:
    name: FunnelName
    by: str
    note: str
    window_days: int
    steps: list[FunnelStep]


def funnel_tab(
    funnel: Funnel,
    by: str,
    current: Sequence[int],
    previous: Sequence[int] | None,
) -> FunnelTab:
    """The tracked steps' counts, then ``None`` for a step nothing tracks."""
    steps = []
    for i, step in enumerate(funnel.steps):
        if i < len(current):
            before = previous[i] if previous is not None else None
            count: Metric | None = compared(current[i], before, previous is not None)
        else:
            count = None
        steps.append(FunnelStep(key=step.key, label=step.label, count=count))
    return FunnelTab(
        name=funnel.name, by=by, note=funnel.note, window_days=funnel.window_days, steps=steps
    )


# --- Event cards -------------------------------------------------------------

#: `mentor_profile_viewed.source` when the view came from a search result:
#: the surfaces share the booking entry points' names (PRD 30).
FROM_SEARCH = EntryPoint.EXPLORE.value
#: `writing_assist_used.outcome` values the card shows.
ACCEPTED, DISCARDED = "accepted", "discarded"
#: The Errors by screen card's length.
TOP_ROUTES = 3


@dataclass(frozen=True, slots=True)
class LoginCodes:
    """Log-in attempts (codes verified or failed) and the failures, all and
    those of migrated users."""

    attempts: int
    failed: int
    failed_migrated: int


@dataclass(frozen=True, slots=True)
class Counted:
    key: str
    count: int


@dataclass(frozen=True, slots=True)
class BehaviourFigures:
    """What the vendor counted in one window, before any rate."""

    login: LoginCodes
    searches: int
    searches_no_results: int
    profile_views_from_search: int
    profile_views: list[Counted]
    #: Every profile view, those in no row shown included.
    profile_views_total: int
    join_clicked: int
    join_failed: int
    median_minutes_from_start: float | None
    assist_used: int
    assist_accepted: int
    assist_discarded: int
    app_installed: int
    push_allowed_people: int
    active_people: int
    errors: list[Counted]


@dataclass(frozen=True, slots=True)
class Share:
    key: str
    share: float | None


@dataclass(frozen=True, slots=True)
class BehaviourTab:
    login_codes: dict[str, int | float | None]
    search: dict[str, int | float | None]
    profile_views_by_source: list[Share]
    joining: dict[str, int | float | None]
    writing_assist: dict[str, int | float | None]
    app: dict[str, int | float | None]
    errors_by_screen: list[Counted]


def behaviour_tab(figures: BehaviourFigures) -> BehaviourTab:
    """The seven cards' rows, rates as percentages (`rate`)."""
    login = figures.login
    return BehaviourTab(
        login_codes={
            "attempts": login.attempts,
            "failed_rate": rate(login.failed, login.attempts),
            "failed_migrated_rate": rate(login.failed_migrated, login.failed),
        },
        search={
            "searches": figures.searches,
            "no_results_rate": rate(figures.searches_no_results, figures.searches),
            "profile_views_from_search": figures.profile_views_from_search,
        },
        profile_views_by_source=[
            Share(key=row.key, share=rate(row.count, figures.profile_views_total))
            for row in figures.profile_views
        ],
        joining={
            "join_clicked": figures.join_clicked,
            "join_failed": figures.join_failed,
            "median_minutes_from_start": rounded(figures.median_minutes_from_start),
        },
        writing_assist={
            "used": figures.assist_used,
            "accepted_rate": rate(figures.assist_accepted, figures.assist_used),
            "discarded_rate": rate(figures.assist_discarded, figures.assist_used),
        },
        app={
            "installed": figures.app_installed,
            "push_allowed_rate": rate(figures.push_allowed_people, figures.active_people),
        },
        errors_by_screen=figures.errors[:TOP_ROUTES],
    )


# --- Explore and matching ----------------------------------------------------

#: A search leads to a booking when one is requested this soon after it (PRD 30).
BOOKED_WITHIN = dt.timedelta(days=7)


@dataclass(frozen=True, slots=True)
class Searchers:
    """People who searched in a window, and those of them who then viewed a
    mentor's profile or started a booking in it."""

    searched: int
    led: int


@dataclass(frozen=True, slots=True)
class ExploreFigures:
    """`Searchers` per asked window, in order, and how many searchers of the
    first window requested a booking within `BOOKED_WITHIN` of their first search."""

    per_window: list[Searchers]
    booked: int


def explore_windows(period: Period) -> list[Window]:
    """What the Explore card reads: the window, the previous period, then
    the window's sparkline parts, as every feature card does."""
    current = period.window
    return [current, period.previous().window, *spark_buckets(current)]


def explore_feature(figures: ExploreFigures, compare: bool) -> Feature:
    """The Explore and matching card from `explore_windows`' figures: searchers
    who went on to a profile or a booking, over searchers; contribution, the
    window's searchers who requested a booking within `BOOKED_WITHIN`."""
    rates = [rate(w.led, w.searched) for w in figures.per_window]
    return Feature(
        key="explore",
        value=compared(rates[0], rates[1], compare),
        spark=rates[2:],
        contribution=figures.booked,
    )


# --- The log-in alert --------------------------------------------------------

#: Failed log-in codes over this share of a day's attempts raise the alert.
LOGIN_FAILED_OVER = Fraction(10, 100)


def _percent(value: float) -> str:
    return f"{value:g}%"


def login_alert(codes: LoginCodes, today: dt.date) -> Alert | None:
    """An alert when more than `LOGIN_FAILED_OVER` of yesterday's (UTC) log-in
    attempts failed, raised at the start of today, adding a sentence when
    migrated users are more than half of the failures."""
    if codes.attempts <= 0 or Fraction(codes.failed, codes.attempts) <= LOGIN_FAILED_OVER:
        return None
    # Rounded up to one decimal, so a share just over the line never reads as it.
    share = math.ceil(Fraction(codes.failed * 1000, codes.attempts)) / 10
    body = (
        f"{_percent(share)} of log-in attempts failed yesterday "
        f"(alert at {_percent(float(LOGIN_FAILED_OVER * 100))})."
    )
    if codes.failed_migrated * 2 > codes.failed:
        body += " Most are migrated users."
    return Alert(
        key="login_code_failed",
        title="Log-in codes failing",
        body=body,
        link=AlertLink(tab="funnels", funnel="signup", by=SignupSource.MIGRATED.value),
        raised_at=dt.datetime(today.year, today.month, today.day, tzinfo=dt.UTC),
    )
