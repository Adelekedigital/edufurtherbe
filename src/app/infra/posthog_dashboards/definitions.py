"""The PostHog dashboards, versioned here (decision 341).

Every funnel is `posthog_reader.funnel_body`, the query admin Analytics sends,
so the two cannot disagree; a dashboard only adds PostHog's own breakdown and a
relative date range, and `insight_query` adds admin Analytics' own exclusion.
Event and property names are the constants of `domain.analytics_behaviour`.
Each dashboard and insight has a ``key`` the sync matches by, so a rename
updates in place: never change one.

Left out on purpose: "Days to bookable" and exact mentor activation are read
from the database (decision 338), and Web Analytics (pageviews) is PostHog's
own page.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any
from uuid import UUID

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
from app.infra.clients.posthog_reader import funnel_body, not_internal, property_filter

#: Marks what the sync owns, on dashboards and insights.
MANAGED_TAG = "edufurther-managed"

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
    #: Stable across renames: what the sync matches by.
    key: str
    name: str
    #: The query source, without its exclusion.
    source: dict[str, Any]
    #: The admin funnel this is, for the test that holds them equal.
    funnel: FunnelName | None = None


@dataclass(frozen=True, slots=True)
class Dashboard:
    key: str
    name: str
    description: str
    insights: tuple[Insight, ...]


def _breakdown(key: str, on: str) -> dict[str, Any]:
    return {"breakdownFilter": {"breakdown": key, "breakdown_type": on}}


def _funnel(
    key: str, name: str, funnel: Funnel, by: tuple[str, str] | None = None, admin: bool = True
) -> Insight:
    source = {**funnel_body(funnel, EVERYONE), "dateRange": dict(LAST_30_DAYS)}
    if by is not None:
        source |= _breakdown(*by)
    return Insight(key, name, source, funnel.name if admin else None)


def _trends(series: list[dict[str, Any]], by: tuple[str, str] | None) -> dict[str, Any]:
    source: dict[str, Any] = {
        "kind": "TrendsQuery",
        "series": series,
        "interval": "day",
        "dateRange": dict(LAST_30_DAYS),
        "trendsFilter": {"display": "ActionsLineGraph"},
    }
    return source | _breakdown(*by) if by is not None else source


def _trend(
    key: str,
    name: str,
    event: str,
    where: tuple[Match, ...] = (),
    by: tuple[str, str] | None = None,
) -> Insight:
    node = {
        "kind": "EventsNode",
        "event": event,
        "math": "total",
        "properties": [property_filter(m) for m in where],
    }
    return Insight(key, name, _trends([node], by))


_BY_ROLE = (ROLE, "person")

#: Daily, weekly and monthly active members over any event, by role: PostHog's
#: `BaseMathType` `dau`, `weekly_active` and `monthly_active`, and an
#: `EventsNode` whose `event` is null, "all events"
#: (https://posthog.com/docs/open-api-spec/insights_create.md).
ACTIVE_USERS = Insight(
    "active_users",
    "Active users (daily, weekly, monthly)",
    _trends(
        [
            {"kind": "EventsNode", "event": None, "math": math, "properties": []}
            for math in ("dau", "weekly_active", "monthly_active")
        ],
        _BY_ROLE,
    ),
)

#: Of those active in a week, who came back in each following week, over
#: eight weeks, by role: a `RetentionQuery` with a `RetentionFilter` (`period`
#: "Week", `totalIntervals`, `retentionType`) whose start and return entities
#: are any event, an `EventsNode` with no event id, and a `breakdownFilter`,
#: which `RetentionQuery` takes (https://posthog.com/docs/open-api-spec/insights_create.md).
_ANY_EVENT = {"id": None, "kind": "EventsNode", "type": "events", "name": "All events"}
WEEKLY_RETENTION = Insight(
    "weekly_retention",
    "Weekly retention",
    {
        "kind": "RetentionQuery",
        "retentionFilter": {
            "period": "Week",
            "totalIntervals": 8,
            "retentionType": "retention_recurring",
            "targetEntity": _ANY_EVENT,
            "returningEntity": _ANY_EVENT,
        },
        "dateRange": {"date_from": "-8w"},
        **_breakdown(*_BY_ROLE),
    },
)

DASHBOARDS: tuple[Dashboard, ...] = (
    Dashboard(
        "acquisition",
        "Acquisition and sign-up",
        "Where members come from and how far they get through sign-up.",
        (
            _funnel(
                "signup_by_source",
                "Sign-up funnel by source",
                FUNNELS["signup"],
                (SIGNUP_SOURCE, "person"),
            ),
            _funnel(
                "signup_by_channel",
                "Sign-up funnel by channel",
                FUNNELS["signup"],
                (SIGNUP_CHANNEL, "person"),
            ),
            _trend(
                "invite_links_shared",
                "Invite links shared",
                LINK_SHARED,
                (Match(KIND, INVITE_LINK),),
            ),
        ),
    ),
    Dashboard(
        "mentee_journey",
        "Mentee journey",
        "From onboarding to a first session, and how bookings go.",
        (
            _funnel("mentee_activation", "Mentee activation", FUNNELS["mentee_activation"]),
            _funnel(
                "booking_by_entry_point",
                "Booking funnel by entry point",
                FUNNELS["booking"],
                (ENTRY_POINT, "event"),
            ),
        ),
    ),
    Dashboard(
        "mentor_journey",
        "Mentor journey",
        "From application to a first session delivered.",
        (
            _funnel("mentor_application", "Mentor application", FUNNELS["mentor_application"]),
            _funnel(
                "mentor_activation_events",
                "Mentor activation (events; admin Analytics is exact)",
                MENTOR_ACTIVATION_EVENTS,
                admin=False,
            ),
            _trend("calendars_connected", "Calendars connected", CALENDAR_CONNECTED),
            _trend(
                "mentor_profile_links_shared",
                "Mentor profile links shared",
                LINK_SHARED,
                (Match(KIND, MENTOR_PROFILE_LINK), Match(OWN, "true")),
            ),
        ),
    ),
    Dashboard(
        "engagement",
        "Engagement",
        "Active members, retention, joining sessions, messages and writing assist, by role.",
        (
            ACTIVE_USERS,
            WEEKLY_RETENTION,
            _trend("join_clicks", "Session join clicks by role", JOIN_CLICKED, by=_BY_ROLE),
            _trend("join_failures", "Session join failures by role", JOIN_FAILED, by=_BY_ROLE),
            _trend("messages_sent", "Messages sent by role", MESSAGE_SENT, by=_BY_ROLE),
            _trend(
                "writing_assist_uses", "Writing assist uses by role", WRITING_ASSIST, by=_BY_ROLE
            ),
        ),
    ),
    Dashboard(
        "reliability",
        "Reliability",
        "Failed requests, server errors and the installed app.",
        (
            _trend(
                "failed_requests", "Failed requests by route", REQUEST_FAILED, by=(ROUTE, "event")
            ),
            _trend("server_errors", "Server errors", EXCEPTION_EVENT),
            _trend("push_granted", "Push permission granted", PUSH_ALLOWED),
            _trend("app_installs", "App installs", APP_INSTALLED),
        ),
    ),
)


def insight_query(insight: Insight, internal: Sequence[UUID]) -> dict[str, Any]:
    """The saved query: admin Analytics' own exclusion of ``internal`` on the
    insight, first as there, and never the project's unreviewed test-account
    filters (decision 331)."""
    source = {
        **insight.source,
        "filterTestAccounts": False,
        "properties": [not_internal(internal), *insight.source.get("properties", [])],
    }
    return {"kind": "InsightVizNode", "source": source}
