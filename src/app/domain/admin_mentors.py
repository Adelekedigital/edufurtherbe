"""The admin Mentors page's rules (Admin PRD, 2026-10-26; decision 297).

Pure: which tab a mentor sits in is SQL (`infra/db/admin_mentors.py`), but how a
show-up rate is banded, how long somebody has waited, what a booking came to and
which admin grant a person "is" are decided here once, so the summary cards, the
rows and the drawer cannot disagree.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from enum import StrEnum

from app.domain.enums import AdminRole, SessionStatus

__all__ = [
    "AT_RISK_BELOW",
    "ON_TRACK_FROM",
    "AdminMentorTab",
    "BookingOutcome",
    "PausedBy",
    "ShowUpTier",
    "booking_outcome",
    "show_up_tier",
    "strongest_role",
    "waiting_days",
]


class AdminMentorTab(StrEnum):
    """The four tabs, each one population of mentors (no mentor is in two)."""

    #: Submitted and not yet decided.
    APPLICATIONS = "applications"
    #: Approved and listed.
    APPROVED = "approved"
    #: Approved and unlisted, by an admin or by themselves.
    PAUSED = "paused"
    #: Declined.
    NOT_APPROVED = "not_approved"


class ShowUpTier(StrEnum):
    """A show-up rate's band: the Mentor Home rule (80+, 50-79, under 50)."""

    ON_TRACK = "on_track"
    NEEDS_ATTENTION = "needs_attention"
    AT_RISK = "at_risk"


#: The lowest rate that is on track.
ON_TRACK_FROM = 80
#: A rate below this is at risk; the summary's `at_risk` counts these.
AT_RISK_BELOW = 50


def show_up_tier(rate: int | None) -> ShowUpTier | None:
    """The band of a whole-percent rate, or ``None`` with no rate (nothing settled)."""
    if rate is None:
        return None
    if rate >= ON_TRACK_FROM:
        return ShowUpTier.ON_TRACK
    if rate >= AT_RISK_BELOW:
        return ShowUpTier.NEEDS_ATTENTION
    return ShowUpTier.AT_RISK


class PausedBy(StrEnum):
    """Who took an approved mentor off Explore."""

    ADMIN = "admin"
    MENTOR = "mentor"


class BookingOutcome(StrEnum):
    """What one booked session came to, as the drawer's recent bookings tag it."""

    UPCOMING = "upcoming"
    #: Confirmed and started, not yet settled: in its join window or just past.
    AWAITING_OUTCOME = "awaiting_outcome"
    COMPLETED = "completed"
    MISSED_BY_MENTOR = "missed_by_mentor"
    MISSED_BY_MENTEE = "missed_by_mentee"
    CANCELED = "canceled"


def booking_outcome(
    status: SessionStatus,
    *,
    starts_at: dt.datetime,
    now: dt.datetime,
    mentor_missed: bool,
    mentee_missed: bool,
) -> BookingOutcome:
    """The tag for one booked session.

    The misses are each party's own participant row (`session_stats.missed_by`),
    so a settled session reads the same here as in the stats beside it. When
    both missed it is the mentor's miss: the drawer is about the mentor. A
    `completed` session nobody missed is completed. A confirmed one is
    `upcoming` until it starts (`pending_requests.upcoming_session`'s rule, so
    it agrees with `stats.upcoming`), then `awaiting_outcome` until settled.
    """
    if status == SessionStatus.CANCELLED:
        return BookingOutcome.CANCELED
    if mentor_missed:
        return BookingOutcome.MISSED_BY_MENTOR
    # A `no_show` with neither row recorded (a migrated session) is not the
    # mentor's miss, which the mentor's stats would not count either.
    if mentee_missed or status == SessionStatus.NO_SHOW:
        return BookingOutcome.MISSED_BY_MENTEE
    if status == SessionStatus.CONFIRMED:
        return BookingOutcome.UPCOMING if starts_at > now else BookingOutcome.AWAITING_OUTCOME
    return BookingOutcome.COMPLETED


def waiting_days(submitted_at: dt.datetime | None, now: dt.datetime) -> int | None:
    """Whole days an application has waited, rounded down; ``None`` unsubmitted."""
    if submitted_at is None:
        return None
    return max((now - submitted_at) // dt.timedelta(days=1), 0)


#: Strongest first: the grant `/me` reports when a person holds several.
_ROLE_ORDER = (AdminRole.SUPER_ADMIN, AdminRole.MENTOR_APPROVAL, AdminRole.LIMITED_ACCESS)


def strongest_role(held: Iterable[str]) -> AdminRole | None:
    """The strongest live grant of the ones held, or ``None`` for none."""
    names = {str(role) for role in held}
    return next((role for role in _ROLE_ORDER if role.value in names), None)
