"""Deleting your own account (`DELETE /me`), in one transaction.

Everything the person was part of that another person could still act on is
ended or taken away **before** the account is stamped deleted: sessions, unused
credits, a mentor listing and offered times. Stamping last keeps each step on a
live account, so the helpers that lock a live row (`lock_mentor`) still find it.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.accounts import RESTORE_WINDOW, UNLISTED_ON_DELETION
from app.domain.enums import MentorStatusType
from app.domain.refunds import RefundPolicy
from app.infra.clients.meetings import CalendarPort
from app.infra.db.credit_expiry import expire_user_credits
from app.infra.db.mentor_status_store import record
from app.infra.db.models.suggestions import SessionSuggestion
from app.infra.db.models.user import User
from app.infra.db.predicates import LIVE
from app.infra.db.session_writer.transitions import end_for_deleted_account


async def delete_account(
    session: AsyncSession,
    user_id: UUID,
    *,
    now: dt.datetime,
    refunds: RefundPolicy,
    calendar: CalendarPort,
) -> bool:
    """Soft-delete this account and everything open on it. ``False`` when it is
    already gone. Does not commit.

    **The account row is locked first.** A booking takes a share lock on both
    parties' rows (`booking.py`), so a booking and a deletion run one after the
    other: either the session exists before the deletion reads its sessions, or
    the booking finds the account gone.
    """
    locked = await session.execute(
        select(User.id).where(User.id == user_id, LIVE).with_for_update()
    )
    if locked.first() is None:
        return False

    await end_for_deleted_account(session, user_id, now=now, refunds=refunds, calendar=calendar)
    # After the sessions: a request the person withdrew refunds a fresh lot,
    # which goes with the rest.
    await expire_user_credits(session, user_id)
    # No mentor profile, no event.
    await record(
        session,
        user_id=user_id,
        status_type=MentorStatusType.UNLISTED,
        created_by=user_id,
        reason=UNLISTED_ON_DELETION,
    )
    # Offered times nobody can book any more; their reminders find nothing.
    await session.execute(
        delete(SessionSuggestion).where(
            SessionSuggestion.accepted_session_id.is_(None),
            or_(SessionSuggestion.mentor_id == user_id, SessionSuggestion.mentee_id == user_id),
        )
    )
    await session.execute(
        update(User)
        .where(User.id == user_id)
        .values(deleted_at=now, deletion_purge_at=now + RESTORE_WINDOW)
    )
    return True
