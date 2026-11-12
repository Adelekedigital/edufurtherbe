"""The PostHog dashboards, versioned here (decision 341).

Every funnel is `posthog_reader.funnel_body`, the query admin Analytics sends,
so the two cannot disagree; a dashboard only adds PostHog's own breakdown, a
relative date range and the exclusion (`insight_query`). Event and property
names are the constants of `domain.analytics_behaviour`.

Left out on purpose: "Days to bookable" and exact mentor activation are read
from the database (decision 338), and Web Analytics (pageviews) is PostHog's
own page.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from app.domain.analytics_behaviour import (
    APP_INSTALLED,
    AVAILABILITY_SET,
    BOOKING_RECEIVED,
    CALENDAR_CONNECTED,
    EVERYONE,
    FUNNELS,
    INVITE_LINK,
    JOIN_CLICKED,
    JOIN_FAILED,
    KIND,
    LINK_SHARED,
    MENTOR_LISTED,
    MENTOR_PROFILE_LINK,
    MESSAGE_SENT,
    OWN,
    PUSH_ALLOWED,
    REQUEST_FAILED,
    ROLE,
    ROUTE,
    SESSION_DELIVERED,
    SESSION_TYPE_CREATED,
    SIGNUP_CHANNEL,
    SIGNUP_SOURCE,
    WRITING_ASSIST,
    Funnel,
    FunnelName,
    Match,
)
from app.domain.analytics_events import ENTRY_POINT
from app.infra.clients.posthog_errors import EXCEPTION_EVENT
from app.infra.clients.posthog_reader import funnel_body, property_filter

#: Marks what the sync owns: a tag on dashboards and insights, a word in the
#: cohort's description (cohorts carry no tags).
MANAGED_TAG = "edufurther-managed"
COHORT_NAME = "Internal accounts (EduFurther)"
COHORT_DESCRIPTION = (
    f"Internal accounts (users.is_internal), synced by scripts/sync_posthog_dashboards.py. "
    f"{MANAGED_TAG}"
)

LAST_30_DAYS = MappingProxyType({"date_from": "-30d"})

#: Mentor activation from events: the database funnel's steps (decision 338)
#: with the event each sends. Setup before approval is not counted here.
_DB_ACTIVATION = FUNNELS["mentor_activation"]
MENTOR_ACTIVATION_EVENTS: Funnel = dataclasses.replace(
    _DB_ACTIVATION,
    steps=tuple(
        dataclasses.replace(step, events=(event,))
        for step, event in zip(
            _DB_ACTIVATION.steps,
            (
                MENTOR_LISTED,
                AVAILABILITY_SET,
                SESSION_TYPE_CREATED,
                BOOKING_RECEIVED,
                SESSION_DELIVERED,
            ),
            strict=True,
        )
    ),
    in_database=False,
)


@dataclass(frozen=True, slots=True)
class Insight:
    name: str
    #: The query source, without its exclusion.
    source: dict[str, Any]
    #: The admin funnel this is, for the test that holds them equal.
    funnel: FunnelName | None = None


@dataclass(frozen=True, slots=True)
class Dashboard:
    name: str
    description: str
    insights: tuple[Insight, ...]


def _breakdown(key: str, on: str) -> dict[str, Any]:
    return {"breakdownFilter": {"breakdown": key, "breakdown_type": on}}


def _funnel(
    name: str, funnel: Funnel, by: tuple[str, str] | None = None, admin: bool = True
) -> Insight:
    source = {**funnel_body(funnel, EVERYONE), "dateRange": dict(LAST_30_DAYS)}
    if by is not None:
        source |= _breakdown(*by)
    return Insight(name, source, funnel.name if admin else None)


def _trend(
    name: str, event: str, where: tuple[Match, ...] = (), by: tuple[str, str] | None = None
) -> Insight:
    source: dict[str, Any] = {
        "kind": "TrendsQuery",
        "series": [
            {
                "kind": "EventsNode",
                "event": event,
                "math": "total",
                "properties": [property_filter(m) for m in where],
            }
        ],
        "interval": "day",
        "dateRange": dict(LAST_30_DAYS),
        "trendsFilter": {"display": "ActionsLineGraph"},
    }
    if by is not None:
        source |= _breakdown(*by)
    return Insight(name, source)


_BY_ROLE = (ROLE, "person")

DASHBOARDS: tuple[Dashboard, ...] = (
    Dashboard(
        "Acquisition and sign-up",
        "Where members come from and how far they get through sign-up.",
        (
            _funnel("Sign-up funnel by source", FUNNELS["signup"], (SIGNUP_SOURCE, "person")),
            _funnel("Sign-up funnel by channel", FUNNELS["signup"], (SIGNUP_CHANNEL, "person")),
            _trend("Invite links shared", LINK_SHARED, (Match(KIND, INVITE_LINK),)),
        ),
    ),
    Dashboard(
        "Mentee journey",
        "From onboarding to a first session, and how bookings go.",
        (
            _funnel("Mentee activation", FUNNELS["mentee_activation"]),
            _funnel("Booking funnel by entry point", FUNNELS["booking"], (ENTRY_POINT, "event")),
        ),
    ),
    Dashboard(
        "Mentor journey",
        "From application to a first session delivered.",
        (
            _funnel("Mentor application", FUNNELS["mentor_application"]),
            _funnel(
                "Mentor activation (events; admin Analytics is exact)",
                MENTOR_ACTIVATION_EVENTS,
                admin=False,
            ),
            _trend("Calendars connected", CALENDAR_CONNECTED),
            _trend(
                "Mentor profile links shared",
                LINK_SHARED,
                (Match(KIND, MENTOR_PROFILE_LINK), Match(OWN, "true")),
            ),
        ),
    ),
    Dashboard(
        "Engagement",
        "Joining sessions, messages and writing assist, by role.",
        (
            _trend("Session join clicks by role", JOIN_CLICKED, by=_BY_ROLE),
            _trend("Session join failures by role", JOIN_FAILED, by=_BY_ROLE),
            _trend("Messages sent by role", MESSAGE_SENT, by=_BY_ROLE),
            _trend("Writing assist uses by role", WRITING_ASSIST, by=_BY_ROLE),
        ),
    ),
    Dashboard(
        "Reliability",
        "Failed requests, server errors and the installed app.",
        (
            _trend("Failed requests by route", REQUEST_FAILED, by=(ROUTE, "event")),
            _trend("Server errors", EXCEPTION_EVENT),
            _trend("Push permission granted", PUSH_ALLOWED),
            _trend("App installs", APP_INSTALLED),
        ),
    ),
)


def excluded(cohort_id: int) -> dict[str, Any]:
    """Leaves the internal accounts' cohort out (PostHog's `CohortPropertyFilter`)."""
    return {"type": "cohort", "key": "id", "value": cohort_id, "operator": "not_in"}


def insight_query(insight: Insight, cohort_id: int) -> dict[str, Any]:
    """The saved query: the source with the internal cohort excluded on the
    insight itself, reviewed here, and the project's test-account filters on."""
    source = {
        **insight.source,
        "filterTestAccounts": True,
        "properties": [*insight.source.get("properties", []), excluded(cohort_id)],
    }
    return {"kind": "InsightVizNode", "source": source}
