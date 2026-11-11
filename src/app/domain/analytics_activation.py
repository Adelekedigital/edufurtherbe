"""Mentor activation (decision 338): mentors approved in a window, their setup
and their first `MENTOR_ACTIVATED_WITHIN`, counted from what the database says
about each one (`infra/db/analytics_activation.py`). Pure.

The path is approved, then weekly hours, then a published session type of
their own, each needing the one before. The outcomes are counted beside it
from approval: a booking request, then a delivered session.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from app.domain.analytics import days_of, rate
from app.domain.analytics_behaviour import FunnelFigure, figure


@dataclass(frozen=True, slots=True)
class ApprovedMentor:
    """One mentor approved in the window, at their first approval in it."""

    user_id: UUID
    approved_at: dt.datetime
    #: When their earliest weekly hours still standing at or after approval
    #: were set; ``None`` with none.
    hours_at: dt.datetime | None
    #: The same for a published session type of their own (not onboarding's).
    own_type_at: dt.datetime | None
    #: A booking request to them within `MENTOR_ACTIVATED_WITHIN` of approval.
    booked: bool
    #: A completed session as mentor in the same time.
    delivered: bool
    #: They have connected a calendar.
    calendar: bool

    @property
    def bookable_at(self) -> dt.datetime | None:
        """When both setup steps held, never before the approval."""
        if self.hours_at is None or self.own_type_at is None:
            return None
        return max(self.approved_at, self.hours_at, self.own_type_at)


@dataclass(frozen=True, slots=True)
class Activation:
    """One window's approved mentors, and how many of them shared their
    profile link (``None`` when PostHog did not answer)."""

    mentors: Sequence[ApprovedMentor]
    shared: int | None


def activation_steps(mentors: Sequence[ApprovedMentor]) -> dict[str, int]:
    """Each `mentor_activation` step's count, by its key."""
    hours = [m for m in mentors if m.hours_at is not None]
    booked = [m for m in mentors if m.booked]
    return {
        "approved": len(mentors),
        "availability_set": len(hours),
        "session_type_created": sum(1 for m in hours if m.own_type_at is not None),
        "first_booking_received": len(booked),
        "first_session_delivered": sum(1 for m in booked if m.delivered),
    }


def days_to_bookable(mentors: Sequence[ApprovedMentor]) -> float | None:
    """The median days from approval to bookable, of those bookable; ``None``
    with none."""
    waits = [
        (at - m.approved_at).total_seconds() for m in mentors if (at := m.bookable_at) is not None
    ]
    return days_of(statistics.median(waits)) if waits else None


def _measures(window: Activation) -> tuple[float | None, float | None, float | None]:
    approved = len(window.mentors)
    connected = sum(1 for m in window.mentors if m.calendar)
    shared = None if window.shared is None else rate(window.shared, approved)
    return days_to_bookable(window.mentors), rate(connected, approved), shared


def activation_figures(current: Activation, previous: Activation | None) -> list[FunnelFigure]:
    """The figures beside the funnel; ``previous`` ``None`` is no compare."""
    now = _measures(current)
    before = _measures(previous) if previous is not None else (None, None, None)
    keys = ("days_to_bookable", "calendar_connected", "profile_link_shared")
    return [
        figure(key, value, prior, previous is not None)
        for key, value, prior in zip(keys, now, before, strict=True)
    ]
