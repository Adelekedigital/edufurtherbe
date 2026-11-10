"""The stored facts the one-off analytics backfill reads (PRD 30 PR 7).

One reader per kind of fact, each yielding plain rows (`id`, `at`, `user_id`
and what that kind's events carry) in ``(at, id)`` order, strictly before
``until`` and after an optional ``(at, id)`` position, a page at a time so
memory stays bounded however long the history is.

**Who is left out is the live rule plus one**: `analytics_scope.counted_user`,
which the live insert filters on too, and a deleted account (`LIVE`), since
nothing about a person who asked to go should leave for a vendor now (PRD 30
section 8). Which rows report which event is `domain.analytics_events`'.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, case, func, select, tuple_
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.domain.enums import CreditReason, MentorStatusType, SessionStatus
from app.infra.db import keyset
from app.infra.db.analytics_scope import counted_user
from app.infra.db.models.credits import CreditLot, CreditTransaction
from app.infra.db.models.mentoring import MentorStatusEvent
from app.infra.db.models.referrals import ReferralUnlock
from app.infra.db.models.reviews import Review
from app.infra.db.models.sessions import Session, SessionEvent
from app.infra.db.models.user import User
from app.infra.db.predicates import LIVE
from app.infra.db.review_stats import review_value
from app.infra.db.session_stats import canceller

__all__ = [
    "PAGE",
    "credit_facts",
    "listing_facts",
    "review_facts",
    "session_event_facts",
    "unlock_facts",
]

#: Rows per round trip.
PAGE = 500

Position = tuple[dt.datetime, UUID]


async def _paged(
    session: AsyncSession,
    statement: Select[Any],
    at: Any,
    row_id: Any,
    user_id: Any,
    *,
    until: dt.datetime,
    after: Position | None,
    page: int,
) -> AsyncIterator[RowMapping]:
    """``statement``'s rows about counted, live people, before ``until``, in
    ``(at, id)`` order after ``after``, ``page`` at a time."""
    base = (
        statement.join(User, User.id == user_id)
        .where(LIVE, counted_user(user_id), at < until)
        .order_by(at, row_id)
        .limit(page)
    )
    position = after
    while True:
        query = base
        if position is not None:
            query = query.where(
                keyset.after(at, row_id, (position[0].isoformat(), position[1]), ascending=True)
            )
        rows = (await session.execute(query)).mappings().all()
        for row in rows:
            yield row
        if len(rows) < page:
            return
        position = (rows[-1]["at"], rows[-1]["id"])


def session_event_facts(
    session: AsyncSession, *, until: dt.datetime, after: Position | None, page: int = PAGE
) -> AsyncIterator[RowMapping]:
    """Each `session_events` row that may report something, about its mentee:
    its statuses, who cancelled (`canceller`, as the live `_report` reads it),
    ``moved_from`` (the status the session a move ended had: the `from_status`
    of its last event, the ending), ``moved_on`` (a later session was moved
    from this one, so its ending was the move's) and ``confirmed_at``.

    ``confirmed_at`` is set on a legacy session's ending: it left `confirmed`
    but no event ever entered it, because the legacy import writes only a
    creation event and a final one (`domain/transform/sessions`). It is the
    creation event's time, as Bubble never recorded the acceptance, so the
    funnel still reads requested, confirmed, outcome."""
    ended = aliased(SessionEvent)
    successor = aliased(Session)
    entered = aliased(SessionEvent)
    created = aliased(SessionEvent)
    never_entered = ~(
        select(entered.id)
        .where(
            entered.session_id == SessionEvent.session_id,
            entered.to_status == SessionStatus.CONFIRMED,
        )
        .exists()
    )
    creation = (
        select(created.created_at)
        .where(created.session_id == SessionEvent.session_id, created.from_status.is_(None))
        .order_by(created.created_at, created.id)
        .limit(1)
        .scalar_subquery()
    )
    confirmed_at = case(
        (and_(SessionEvent.from_status == SessionStatus.CONFIRMED, never_entered), creation)
    )
    moved_from = (
        select(ended.from_status)
        .where(ended.session_id == Session.rescheduled_from_session_id)
        .order_by(ended.created_at.desc(), ended.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    moved_on = (
        select(successor.id).where(successor.rescheduled_from_session_id == Session.id).exists()
    )
    statement = (
        select(
            SessionEvent.id.label("id"),
            SessionEvent.created_at.label("at"),
            Session.mentee_id.label("user_id"),
            Session.id.label("session_id"),
            Session.session_type_id,
            SessionEvent.from_status,
            SessionEvent.to_status,
            SessionEvent.reason_code,
            canceller(SessionEvent.actor_id, SessionEvent.actor_type).label("by"),
            moved_from.label("moved_from"),
            moved_on.label("moved_on"),
            confirmed_at.label("confirmed_at"),
        )
        .select_from(SessionEvent)
        .join(Session, Session.id == SessionEvent.session_id)
        .where(
            SessionEvent.from_status.is_(None)
            | SessionEvent.to_status.in_(
                [
                    SessionStatus.CONFIRMED,
                    SessionStatus.CANCELLED,
                    SessionStatus.COMPLETED,
                    SessionStatus.NO_SHOW,
                ]
            )
        )
    )
    return _paged(
        session,
        statement,
        SessionEvent.created_at,
        SessionEvent.id,
        Session.mentee_id,
        until=until,
        after=after,
        page=page,
    )


def credit_facts(
    session: AsyncSession, *, until: dt.datetime, after: Position | None, page: int = PAGE
) -> AsyncIterator[RowMapping]:
    """Each grant and each booking's spend in the ledger, with its lot's source."""
    statement = (
        select(
            CreditTransaction.id.label("id"),
            CreditTransaction.created_at.label("at"),
            CreditTransaction.user_id.label("user_id"),
            CreditTransaction.reason,
            CreditTransaction.delta,
            CreditTransaction.session_id,
            CreditLot.source,
        )
        .select_from(CreditTransaction)
        .join(CreditLot, CreditLot.id == CreditTransaction.credit_lot_id)
        .where(CreditTransaction.reason.in_([CreditReason.GRANT, CreditReason.SESSION_BOOKED]))
    )
    return _paged(
        session,
        statement,
        CreditTransaction.created_at,
        CreditTransaction.id,
        CreditTransaction.user_id,
        until=until,
        after=after,
        page=page,
    )


def listing_facts(
    session: AsyncSession, *, until: dt.datetime, after: Position | None, page: int = PAGE
) -> AsyncIterator[RowMapping]:
    """Each `listed` event that took a mentor from not listed to listed: the
    one before it on the listing dimension was `unlisted`, or there was none
    (a profile starts unlisted). Listing a listed mentor is no news."""
    before = aliased(MentorStatusEvent)
    listing = [MentorStatusType.LISTED, MentorStatusType.UNLISTED]
    previous = (
        select(before.status_type)
        .where(
            before.mentor_user_id == MentorStatusEvent.mentor_user_id,
            before.status_type.in_(listing),
            tuple_(before.created_at, before.id)
            < tuple_(MentorStatusEvent.created_at, MentorStatusEvent.id),
        )
        .order_by(before.created_at.desc(), before.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    statement = select(
        MentorStatusEvent.id.label("id"),
        MentorStatusEvent.created_at.label("at"),
        MentorStatusEvent.mentor_user_id.label("user_id"),
    ).where(
        and_(
            MentorStatusEvent.status_type == MentorStatusType.LISTED,
            func.coalesce(previous, MentorStatusType.UNLISTED) != MentorStatusType.LISTED,
        )
    )
    return _paged(
        session,
        statement,
        MentorStatusEvent.created_at,
        MentorStatusEvent.id,
        MentorStatusEvent.mentor_user_id,
        until=until,
        after=after,
        page=page,
    )


def review_facts(
    session: AsyncSession, *, until: dt.datetime, after: Position | None, page: int = PAGE
) -> AsyncIterator[RowMapping]:
    """Each review, about its reviewer, with its value (`review_value`). One
    since deleted still was submitted."""
    statement = select(
        Review.id.label("id"),
        Review.created_at.label("at"),
        Review.reviewed_by.label("user_id"),
        review_value().label("rating"),
    )
    return _paged(
        session,
        statement,
        Review.created_at,
        Review.id,
        Review.reviewed_by,
        until=until,
        after=after,
        page=page,
    )


def unlock_facts(
    session: AsyncSession, *, until: dt.datetime, after: Position | None, page: int = PAGE
) -> AsyncIterator[RowMapping]:
    """Each referral unlock an invite earned. A grandfathered one (no
    referral) was written by the migration, not unlocked, so it is no event."""
    statement = select(
        ReferralUnlock.id.label("id"),
        ReferralUnlock.unlocked_at.label("at"),
        ReferralUnlock.user_id.label("user_id"),
    ).where(ReferralUnlock.unlocked_by_referral_id.is_not(None))
    return _paged(
        session,
        statement,
        ReferralUnlock.unlocked_at,
        ReferralUnlock.id,
        ReferralUnlock.user_id,
        until=until,
        after=after,
        page=page,
    )
