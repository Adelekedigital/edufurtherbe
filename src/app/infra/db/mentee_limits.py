"""The mentee booking limits (#342), read and enforced inside a booking.

The rule itself is :func:`app.domain.booking_limits.limit_breached`. This module
reads the mentee's live sessions and holds the lock that makes the read safe.

**The lock is taken before the read, and that ordering is the guarantee** — the
same reasoning as ``credit_writer.spend_credit``. Two bookings arriving together
would otherwise both count one live session, both pass a cap of two, and both
insert. ``pg_advisory_xact_lock`` keyed on the mentee serialises them, so the
second reads the row the first one wrote.

The overlap rule has a second wall: ``sessions_no_mentee_double_booking`` is an
``EXCLUDE`` constraint, so even a path that skipped this check cannot write two
overlapping live sessions for one mentee. The count and the per-mentor rule have
no such wall, because a constraint cannot count; the lock is what holds them.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.booking_limits import LiveBooking, limit_breached
from app.infra.db.models.sessions import LIVE_STATUSES, Session

__all__ = ["MENTEE_LIMIT_LOCK_NAMESPACE", "check_mentee_limits"]

#: ``'MLIM'`` as four ASCII bytes. Its own namespace, so this lock never
#: collides with the credit lock ``spend_credit`` takes on the same mentee.
MENTEE_LIMIT_LOCK_NAMESPACE = 0x4D4C494D


async def check_mentee_limits(
    session: AsyncSession,
    mentee_id: UUID,
    *,
    mentor_id: UUID,
    starts_at: dt.datetime,
    duration_minutes: int,
) -> None:
    """Raise the limit this booking breaches, or return. Does not commit.

    Call it inside the booking's transaction and before the insert: the lock is
    held until that transaction ends.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:ns, hashtext(:key))"),
        {"ns": MENTEE_LIMIT_LOCK_NAMESPACE, "key": str(mentee_id)},
    )
    rows = (
        await session.execute(
            select(Session.mentor_id, Session.starts_at, Session.duration_minutes).where(
                Session.mentee_id == mentee_id, text(LIVE_STATUSES)
            )
        )
    ).all()
    breach = limit_breached(
        (LiveBooking(r.mentor_id, r.starts_at, r.duration_minutes) for r in rows),
        mentor_id=mentor_id,
        starts_at=starts_at,
        duration_minutes=duration_minutes,
    )
    if breach is not None:
        raise breach
