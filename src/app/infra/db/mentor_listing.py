"""Who is paused, by whom: the listing predicates the store and the outbox share.

Its own module so the outbox — which `mentor_status_store` writes to — can ask
whether a queued return reminder is still due without importing the store back.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.domain.enums import ApprovalStatus, ListingStatus, MentorStatusType, UnlistedReason
from app.infra.db.models.mentoring import MentorProfile, MentorStatusEvent


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


async def return_reminder_still_due(
    session: AsyncSession, user_id: UUID, payload: dict[str, Any]
) -> bool:
    """Whether a queued return reminder still says something true.

    Checked **at send time**: a reminder claimed and queued can wait for a
    retry, and by then the mentor may have resumed, been unlisted by an admin,
    or set a new date. Due only if they are still approved, still paused by
    themselves, and still returning on the date the message was queued for.
    """
    queued_for = payload.get("return_on")
    if queued_for is None:
        return False
    found = await session.execute(
        select(MentorProfile.user_id).where(
            MentorProfile.user_id == user_id,
            MentorProfile.deleted_at.is_(None),
            MentorProfile.approval_status == ApprovalStatus.APPROVED,
            MentorProfile.return_on == dt.date.fromisoformat(str(queued_for)),
            paused_by_mentor(),
        )
    )
    return found.first() is not None
