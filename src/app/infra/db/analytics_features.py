"""The admin Analytics Overview's feature North Stars (PRD 31, decision 324).

Each rate feature is a numerator and a denominator, each a function of a
window returning a scalar subquery, so the value, the previous value and the
eight sparkline points are one statement (`analytics_scope.per_window`).

* **booking**: requests made in the window that became completed sessions
  (`analytics_sessions.requests`, through the move chain).
* **messaging**: chats whose first message was sent in the window that had a
  reply from the other person within 24 hours.
* **invites**: invites created in the window whose invitee finished their
  profile (`qualified_at`).
* **mentee_activation**: mentees who signed up in the window (migrated accounts
  are not sign-ups) with a completed session starting within 14 days.
* **mentor_activation**: mentors approved in the window who are taking
  bookings now. Since decision 270 an application cannot be submitted until
  the mentor is set up to be booked, so approval already means bookable; what
  the figure catches is a mentor who has since stopped being bookable.
* **mentor_supply** is a now-number (`mentor_supply`): listed mentors whose
  card vouches a free time within 14 days, over listed mentors.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import and_, func, select, tuple_
from sqlalchemy.orm import aliased

from app.domain.availability import BookingWindow
from app.domain.enums import MentorStatusType, PrimaryRole, SessionStatus
from app.domain.mentor_stats import Window
from app.infra.db.analytics_scope import Measure, counted_session, counted_user
from app.infra.db.analytics_sessions import is_request, requests
from app.infra.db.chat_store import pair
from app.infra.db.models.availability import MentorNextAvailability
from app.infra.db.models.mentoring import MentorProfile, MentorStatusEvent
from app.infra.db.models.messaging import Conversation, Message
from app.infra.db.models.referrals import Referral
from app.infra.db.models.sessions import Session, SessionEvent
from app.infra.db.models.user import User
from app.infra.db.next_available_state import free_within
from app.infra.db.predicates import during
from app.infra.db.public_visibility import mentor_is_public, taking_bookings
from app.infra.db.session_stats import completed

#: How soon a chat's first message must be answered to count.
REPLY_WITHIN = dt.timedelta(hours=24)
#: How soon a new mentee must have a completed session to count as activated.
MENTEE_ACTIVATED_WITHIN = dt.timedelta(days=14)
#: How soon an approved mentor's first session counts toward sessions.
MENTOR_FIRST_SESSION_WITHIN = dt.timedelta(days=30)
#: Mentor supply looks this many days ahead.
SUPPLY_DAYS = 14


def _scalar(query: Any) -> Any:
    return query.scalar_subquery()


# --- booking ---------------------------------------------------------------


def booking() -> tuple[Measure, Measure]:
    return (lambda w: requests(w, became=SessionStatus.COMPLETED), requests)


# --- messaging -------------------------------------------------------------


def _chats_started(window: Window, *, answered: bool) -> Any:
    """Chats whose first message (not deleted) was sent in ``window``: a message
    in the window with no earlier one in its chat, so the read is bounded by the
    window rather than by every message ever sent."""
    first, earlier = aliased(Message), aliased(Message)
    where = [
        first.deleted_at.is_(None),
        during(first.sent_at, window),
        ~select(earlier.id)
        .where(
            earlier.conversation_id == first.conversation_id,
            earlier.deleted_at.is_(None),
            tuple_(earlier.sent_at, earlier.id) < tuple_(first.sent_at, first.id),
        )
        .exists(),
        counted_user(Conversation.mentor_user_id),
        counted_user(Conversation.mentee_user_id),
    ]
    if answered:
        reply = aliased(Message)
        where.append(
            select(reply.id)
            .where(
                reply.conversation_id == first.conversation_id,
                reply.deleted_at.is_(None),
                reply.sender_id != first.sender_id,
                reply.sent_at > first.sent_at,
                reply.sent_at <= first.sent_at + REPLY_WITHIN,
            )
            .exists()
        )
    return _scalar(
        select(func.count())
        .select_from(first)
        .join(Conversation, Conversation.id == first.conversation_id)
        .where(*where)
    )


def messaging() -> tuple[Measure, Measure]:
    return (
        lambda w: _chats_started(w, answered=True),
        lambda w: _chats_started(w, answered=False),
    )


def _confirmed_in(window: Window, *, messaged_first: bool) -> Any:
    """Sessions that became confirmed in ``window``; with ``messaged_first``,
    only those whose two people had messaged before."""
    where = [
        SessionEvent.to_status == SessionStatus.CONFIRMED,
        during(SessionEvent.created_at, window),
        # A move confirmed again is the same request, not a new confirmation.
        is_request(),
    ]
    if messaged_first:
        where.append(
            select(Message.id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                pair(Session.mentor_id, Session.mentee_id),
                Message.deleted_at.is_(None),
                Message.sent_at < SessionEvent.created_at,
            )
            .exists()
        )
    return _scalar(
        select(func.count())
        .select_from(SessionEvent)
        .join(Session, Session.id == SessionEvent.session_id)
        .where(*where)
    )


def messaged_first() -> tuple[Measure, Measure]:
    """Messaging's contribution: confirmed sessions with a message first."""
    return (
        lambda w: _confirmed_in(w, messaged_first=True),
        lambda w: _confirmed_in(w, messaged_first=False),
    )


# --- invites ---------------------------------------------------------------


def invites_made(window: Window, *, extra: Any = None) -> Any:
    """Invites created in ``window`` by a counted inviter; with ``extra``, only
    those it holds for."""
    where = [during(Referral.created_at, window), counted_user(Referral.referrer_id)]
    if extra is not None:
        where.append(extra)
    return _scalar(select(func.count()).select_from(Referral).where(*where))


def qualified() -> Any:
    """The invitee finished their profile."""
    return Referral.qualified_at.is_not(None)


def invitee_had(*where: Any) -> Any:
    """The invite's invitee has a session as a mentee, matching ``where``."""
    return select(Session.id).where(Session.mentee_id == Referral.invitee_user_id, *where).exists()


def invites() -> tuple[Measure, Measure]:
    return (lambda w: invites_made(w, extra=qualified()), invites_made)


def invitees_who_met(window: Window) -> Any:
    """Invites' contribution: invitees from invites created in ``window`` with
    a completed session as a mentee."""
    return invites_made(window, extra=invitee_had(completed(), counted_session()))


# --- activation ------------------------------------------------------------


def is_sign_up() -> Any:
    """A counted account made here: a migrated account is not a sign-up."""
    return and_(User.legacy_bubble_id.is_(None), counted_user(User.id))


def sign_ups(window: Window, *, role: PrimaryRole | None = None, extra: Any = None) -> Any:
    """Sign-ups made in ``window``; with ``role``, only that `primary_role`."""
    where = [is_sign_up(), during(User.created_at, window)]
    if role is not None:
        where.append(User.primary_role == role)
    if extra is not None:
        where.append(extra)
    return _scalar(select(func.count()).select_from(User).where(*where))


def new_mentees(window: Window, *, activated: bool = False) -> Any:
    extra = None
    if activated:
        extra = (
            select(Session.id)
            .where(
                Session.mentee_id == User.id,
                completed(),
                counted_session(),
                Session.starts_at < User.created_at + MENTEE_ACTIVATED_WITHIN,
            )
            .exists()
        )
    return sign_ups(window, role=PrimaryRole.MENTEE, extra=extra)


def mentee_activation() -> tuple[Measure, Measure]:
    return (lambda w: new_mentees(w, activated=True), new_mentees)


def _approvals(window: Window, *, extra: Any = None) -> Any:
    event = MentorStatusEvent
    where = [
        event.status_type == MentorStatusType.APPROVED,
        during(event.created_at, window),
        counted_user(event.mentor_user_id),
    ]
    if extra is not None:
        where.append(extra)
    return _scalar(
        select(func.count(func.distinct(event.mentor_user_id)))
        .select_from(event)
        .join(MentorProfile, MentorProfile.user_id == event.mentor_user_id)
        .join(User, User.id == event.mentor_user_id)
        .where(*where)
    )


def mentor_activation() -> tuple[Measure, Measure]:
    return (lambda w: _approvals(w, extra=taking_bookings()), _approvals)


def mentors_who_held(window: Window) -> Any:
    """Mentor activation's contribution: mentors approved in ``window`` with a
    completed session starting within 30 days of approval."""
    event = MentorStatusEvent
    return _approvals(
        window,
        extra=select(Session.id)
        .where(
            Session.mentor_id == event.mentor_user_id,
            completed(),
            counted_session(),
            Session.starts_at >= event.created_at,
            Session.starts_at < event.created_at + MENTOR_FIRST_SESSION_WITHIN,
        )
        .exists(),
    )


# --- mentor supply (now) ---------------------------------------------------


def mentor_supply(booking_window: BookingWindow) -> tuple[Any, Any]:
    """Listed mentors whose card vouches a free time within `SUPPLY_DAYS`, and
    listed mentors, now."""

    def listed(extra: Any = None) -> Any:
        where = [*mentor_is_public(), counted_user(MentorProfile.user_id)]
        if extra is not None:
            where.append(extra)
        return _scalar(
            select(func.count())
            .select_from(MentorProfile)
            .join(User, User.id == MentorProfile.user_id)
            .outerjoin(
                MentorNextAvailability,
                MentorNextAvailability.mentor_user_id == MentorProfile.user_id,
            )
            .where(*where)
        )

    return listed(free_within(booking_window, SUPPLY_DAYS)), listed()
