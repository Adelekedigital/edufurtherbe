"""Which requests still wait on a mentor's answer, and which have lapsed.

**One deadline clause, two readers.** The expiry sweep turns a lapsed request
into `expired`; the caller's `/me` badge counts the requests still awaiting
them. Each is the other's complement among `pending_mentor_approval` rows, so
they share `_past_deadline` rather than restating it — a request is never both,
and never neither.

A request with no `respond_by` never lapses: the sweep has always skipped it,
so the badge counts it for as long as it stays pending.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, not_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import SessionStatus
from app.infra.db.models.sessions import Session

__all__ = ["awaiting_mentor", "lapsed_request", "mentor_pending_bookings"]


def _past_deadline(now: dt.datetime) -> Any:
    return and_(Session.respond_by.is_not(None), Session.respond_by <= now)


def lapsed_request(now: dt.datetime) -> list[Any]:
    """A request the mentor let run past its deadline — the sweep's target."""
    return [Session.status == SessionStatus.PENDING_MENTOR_APPROVAL, _past_deadline(now)]


def awaiting_mentor(now: dt.datetime) -> list[Any]:
    """A request the mentor can still answer."""
    return [Session.status == SessionStatus.PENDING_MENTOR_APPROVAL, not_(_past_deadline(now))]


async def mentor_pending_bookings(session: AsyncSession, mentor_id: UUID, now: dt.datetime) -> int:
    """How many requests wait on `mentor_id`'s answer — the Bookings badge.

    Scoped to `mentor_id` in the query: a dual-role user's own requests *as a
    mentee* wait on somebody else. Served by `ix_sessions_mentor_upcoming`,
    partial on the live statuses, which include this one.
    """
    result = await session.execute(
        select(func.count())
        .select_from(Session)
        .where(Session.mentor_id == mentor_id, *awaiting_mentor(now))
    )
    return int(result.scalar_one())
