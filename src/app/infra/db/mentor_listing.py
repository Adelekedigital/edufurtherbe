"""Who is paused, by whom: the listing predicates the store and the outbox share.

Its own module so the outbox — which `mentor_status_store` writes to — can ask
whether a queued return reminder is still due without importing the store back.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import ColumnElement, Time, and_, exists, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.domain.enums import ApprovalStatus, ListingStatus, MentorStatusType, UnlistedReason
from app.infra.db.models.mentoring import MentorProfile, MentorStatusEvent
from app.infra.db.models.user import User
from app.infra.db.predicates import LIVE

#: The local hour from which a mentor's return-day reminder may go out.
RETURN_REMINDER_HOUR = 8


def _newest_unlisting_id() -> Any:
    """The id of this mentor's newest unlisting, correlated to `MentorProfile`."""
    newest = aliased(MentorStatusEvent)
    return (
        select(newest.id)
        .where(
            newest.mentor_user_id == MentorProfile.user_id,
            newest.status_type == MentorStatusType.UNLISTED,
        )
        .order_by(newest.created_at.desc(), newest.id.desc())
        .limit(1)
        .correlate(MentorProfile)
        .scalar_subquery()
    )


def newest_unlisting_is_self() -> ColumnElement[bool]:
    """**The one definition of "the mentor took themselves off".**

    The newest unlisting says `mentor_paused` **and** was written by the mentor —
    or by the migration, which acted for nobody (`created_by` NULL) and carried
    legacy self-pauses. The actor matters, not just the spelling: an admin's
    reason is free text, and an admin who typed `mentor_paused` must not hand the
    mentor the resume button. Correlated to `MentorProfile`; it is what resume,
    the pause guard and `paused_by_mentor` all read (#75).
    """
    event = aliased(MentorStatusEvent)
    return exists(
        select(event.id)
        .where(
            event.id == _newest_unlisting_id(),
            event.reason == UnlistedReason.MENTOR_PAUSED.value,
            or_(event.created_by.is_(None), event.created_by == event.mentor_user_id),
        )
        .correlate(MentorProfile)
    )


def paused_by_mentor() -> ColumnElement[bool]:
    """Currently unlisted, and by their own pause. Correlated to `MentorProfile`."""
    return and_(MentorProfile.listing_status == ListingStatus.UNLISTED, newest_unlisting_is_self())


def reminder_eligible() -> ColumnElement[bool]:
    """**The one definition of "this mentor can still be told to come back".**

    A live account and profile, approved (resume needs approval), still paused
    by themselves, with a return date. Read by the claim and by the send-time
    check alike, so the two cannot drift: they did once, when only one of them
    read `LIVE`. Needs `User` joined to `MentorProfile`.
    """
    return and_(
        LIVE,
        MentorProfile.deleted_at.is_(None),
        MentorProfile.return_on.is_not(None),
        MentorProfile.approval_status == ApprovalStatus.APPROVED,
        paused_by_mentor(),
    )


def return_reminder_due(now: dt.datetime) -> ColumnElement[bool]:
    """**The one definition of "the return morning has come".**

    `RETURN_REMINDER_HOUR` on the return date, in the mentor's **current** zone:
    `date + time` is a local timestamp, compared with their local now. Read by
    the claim and again at send time, so a zone changed in between moves the
    reminder rather than sending it on the wrong local day. Needs `User` joined
    to `MentorProfile`.
    """
    local_now = func.timezone(User.timezone, now)
    due = MentorProfile.return_on + literal(dt.time(RETURN_REMINDER_HOUR), Time)
    return local_now >= due


#: What a queued return reminder should do now.
ReminderState = Literal["due", "wait", "stale"]


async def return_reminder_state(
    session: AsyncSession, user_id: UUID, payload: dict[str, Any], now: dt.datetime
) -> ReminderState:
    """Whether a queued return reminder should be sent, kept, or dropped.

    Checked **at send time**: a reminder claimed and queued can wait for a
    retry, and by then the mentor may have resumed, been unlisted by an admin,
    set a new date, or moved to another zone.

    * `stale`: no longer approved, no longer paused by themselves, or returning
      on a different date than the one queued. The message would be false.
    * `wait`: still true, but not yet the return morning in their current
      zone. Left pending for a later run rather than sent a day early.
    * `due`: send it.

    **Not locked, deliberately.** A resume committing in the instant between
    this check and the send can still let one reminder out: closing that would
    mean holding the profile row across the email provider's HTTP call, which
    blocks every transition on the mentor for the whole drain (#225).
    """
    queued_for = payload.get("return_on")
    if queued_for is None:
        return "stale"
    row = (
        await session.execute(
            select(return_reminder_due(now))
            .select_from(MentorProfile)
            .join(User, User.id == MentorProfile.user_id)
            .where(
                MentorProfile.user_id == user_id,
                reminder_eligible(),
                MentorProfile.return_on == dt.date.fromisoformat(str(queued_for)),
            )
        )
    ).first()
    if row is None:
        return "stale"
    return "due" if row[0] else "wait"
