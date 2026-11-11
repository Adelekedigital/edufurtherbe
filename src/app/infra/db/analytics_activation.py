"""The mentor activation funnel's rows (decision 338): each mentor approved in
a window, at their first approval in it, and what the database says of them.

* **cohort**: `analytics_features.approved_in`, Overview's: approvals in the
  window (read in its `tz`) of a counted mentor, so internal accounts are out.
* **hours** and **own types**: `public_visibility.weekly_hours` and
  `offerings(own=True)` with ``since`` the approval, a milestone: every row
  that stood at or after it, with when it stood, so the domain can tell when
  both first stood together. An own type is any but onboarding's (`is_starter`).
* **booked** and **delivered**: `received_soon` and `delivered_soon`, a request
  and a completed session within `MENTOR_ACTIVATED_WITHIN` of approval.
* **calendar**: a calendar connected before that time ran out, a revoked one
  included: they connected.
* **consented**: `analytics_consent_store.consent_accepted`, who PostHog knows.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Any

from sqlalchemy import select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.analytics_activation import ApprovedMentor, Stood
from app.domain.analytics_behaviour import MENTOR_ACTIVATED_WITHIN, Breakdown, Match
from app.domain.mentor_stats import Window
from app.infra.db.analytics_consent_store import consent_accepted
from app.infra.db.analytics_features import approved_in, delivered_soon, received_soon
from app.infra.db.models.availability import CalendarConnection
from app.infra.db.models.mentoring import MentorStatusEvent
from app.infra.db.models.user import User
from app.infra.db.public_visibility import offerings, weekly_hours

#: The person properties a database funnel can be broken down by, as columns.
PERSON_COLUMNS = {"signup_source": User.signup_source}

Spans = dict[tuple[Any, Any], list[Stood]]


def person_filter(match: Match) -> Any:
    """``match`` as a condition on `User`; a property no column holds is a
    `ValueError`, so a breakdown cannot be offered that nothing reads. A
    negated match also keeps a null, as PostHog's `is_not` does."""
    if match.on != "person" or match.key not in PERSON_COLUMNS or match.value is None:
        raise ValueError(f"not readable from the database: {match}")
    column = PERSON_COLUMNS[match.key]
    return column.is_distinct_from(match.value) if match.negate else column == match.value


def breakdown_filter(breakdown: Breakdown) -> list[Any]:
    """The people a breakdown keeps; a step's breakdown has no database form."""
    if breakdown.step is not None:
        raise ValueError("a database funnel has no step breakdown")
    return [person_filter(match) for match in breakdown.matches]


async def _spans(session: AsyncSession, window: Window, keep: list[Any], rows: Any) -> Spans:
    """When each of ``rows`` (correlated to the profile and the approval)
    stood, by (mentor, approval)."""
    event = MentorStatusEvent
    stood = rows.correlate(MentorStatusEvent).lateral()
    since, until = list(stood.c)
    query = (
        approved_in(window, event.mentor_user_id, event.created_at, since, until)
        .join(stood, true())
        .where(*keep)
    )
    found: Spans = defaultdict(list)
    for mentor, approved, start, end in (await session.execute(query)).all():
        found[mentor, approved].append(Stood(since=start, until=end))
    return found


async def approvals(
    session: AsyncSession, window: Window, breakdown: Breakdown
) -> list[ApprovedMentor]:
    """Every mentor approved in ``window`` that ``breakdown`` keeps, with no
    setup read yet (`with_setup`): enough to ask PostHog about them."""
    event = MentorStatusEvent
    approved = event.created_at
    calendar = select(CalendarConnection.id).where(
        CalendarConnection.user_id == event.mentor_user_id,
        CalendarConnection.connected_at < approved + MENTOR_ACTIVATED_WITHIN,
    )
    query = (
        approved_in(
            window,
            event.mentor_user_id,
            approved,
            received_soon(event.mentor_user_id, approved),
            delivered_soon(event.mentor_user_id, approved),
            calendar.exists(),
            consent_accepted(event.mentor_user_id),
        )
        .where(*breakdown_filter(breakdown))
        .distinct(event.mentor_user_id)
        .order_by(event.mentor_user_id, approved)
    )
    return [
        ApprovedMentor(
            user_id=mentor,
            approved_at=at,
            hours=[],
            own_types=[],
            booked=bool(booked),
            delivered=bool(delivered),
            calendar=bool(connected),
            consented=bool(consented),
        )
        for mentor, at, booked, delivered, connected, consented in (
            await session.execute(query)
        ).all()
    ]


async def with_setup(
    session: AsyncSession, window: Window, breakdown: Breakdown, mentors: list[ApprovedMentor]
) -> list[ApprovedMentor]:
    """``mentors`` (`approvals` of the same window and breakdown) with the
    hours and own types that stood at or after each approval."""
    if not mentors:
        return []
    keep = breakdown_filter(breakdown)
    since = MentorStatusEvent.created_at
    general, dedicated = weekly_hours(since=since)
    hours = [await _spans(session, window, keep, q) for q in (general, dedicated)]
    owned = await _spans(session, window, keep, offerings(since=since, own=True))
    return [
        replace(
            m,
            hours=[span for h in hours for span in h.get((m.user_id, m.approved_at), [])],
            own_types=owned.get((m.user_id, m.approved_at), []),
        )
        for m in mentors
    ]
