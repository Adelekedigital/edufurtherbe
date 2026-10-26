"""A mentee reminding the mentor about their pending request (decision 294).

**One conditional write is the whole guard.** The `UPDATE` names the caller as
the mentee, the request as still awaiting the mentor (`awaiting_mentor`, the
deadline rule accept and decline use) and the reminder as unsent, and stamps
`reminder_sent_at` in the same statement. Two taps at once both reach it; the
row lock makes the second re-read the row after the first commits, find the
stamp, and match nothing. So exactly one email is queued, with no pre-check
to race (decision 169).

Only on a miss is the session read again, scoped to the caller as a party
(non-negotiable #5), to say which refusal it was.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    NotFoundError,
    ReminderAlreadySentError,
    ReminderMenteeOnlyError,
    RequestNotPendingError,
)
from app.domain.notifications import Notification
from app.infra.db.models.sessions import Session
from app.infra.db.outbox import enqueue
from app.infra.db.pending_requests import awaiting_mentor
from app.infra.db.session_store import is_a_party

__all__ = ["send_request_reminder"]


async def send_request_reminder(
    session: AsyncSession, session_id: UUID, caller: UUID, *, message: str, now: dt.datetime
) -> None:
    """Stamp the reminder and queue the mentor's email, or say why not. Does not commit.

    Raises, in this order: :class:`NotFoundError` when the caller is not a party
    (or there is no such session); :class:`ReminderMenteeOnlyError` when the
    caller is the mentor; :class:`RequestNotPendingError` when the request no
    longer awaits an answer; :class:`ReminderAlreadySentError` when it was
    already reminded. Not-pending comes first: when both apply, the state of
    the request is what the mentee needs to hear.
    """
    mentor_id = await session.scalar(
        update(Session)
        .where(
            Session.id == session_id,
            Session.mentee_id == caller,
            *awaiting_mentor(now),
            Session.reminder_sent_at.is_(None),
        )
        .values(reminder_sent_at=now)
        .returning(Session.mentor_id)
    )
    if mentor_id is None:
        target = (
            await session.execute(
                select(
                    Session.mentee_id,
                    and_(*awaiting_mentor(now)).label("awaiting"),
                ).where(Session.id == session_id, is_a_party(caller))
            )
        ).one_or_none()
        if target is None:
            raise NotFoundError("no such session")
        if target.mentee_id != caller:
            raise ReminderMenteeOnlyError("only the mentee may remind the mentor")
        if not target.awaiting:
            raise RequestNotPendingError("this request no longer awaits an answer")
        raise ReminderAlreadySentError("you have already reminded the mentor")

    await enqueue(
        session,
        Notification.MENTEE_REQUEST_REMINDER,
        entity_type="session",
        entity_id=session_id,
        recipient_ids=(mentor_id,),
        # No `kind`: the column above is the once-only guard, and no retrying
        # scheduler enqueues this.
        variables={"reminder_message": message},
    )
