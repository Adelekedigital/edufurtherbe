"""Which requests still wait on a mentor's answer, which have lapsed, which
sessions are upcoming — and the caller's counts of each, per role.

**One deadline clause, three readers.** The expiry sweep turns a lapsed request
into `expired`; the mentor's accept and decline refuse one; `/me` counts the
requests still awaiting an answer. Awaiting and lapsed are complements among
`pending_mentor_approval` rows, so they share `_past_deadline` rather than
restating it — a request is never both, and never neither.

A request with no `respond_by` never lapses: the sweep has always skipped it,
so it counts as awaiting for as long as it stays pending.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, not_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import SessionStatus
from app.infra.db.models.sessions import LIVE_STATUSES, Session

__all__ = ["awaiting_mentor", "booking_counts", "lapsed_request", "upcoming_session"]


def _past_deadline(now: dt.datetime) -> Any:
    return and_(Session.respond_by.is_not(None), Session.respond_by <= now)


def lapsed_request(now: dt.datetime) -> list[Any]:
    """A request the mentor let run past its deadline — the sweep's target."""
    return [Session.status == SessionStatus.PENDING_MENTOR_APPROVAL, _past_deadline(now)]


def awaiting_mentor(now: dt.datetime) -> list[Any]:
    """A request the mentor can still answer."""
    return [Session.status == SessionStatus.PENDING_MENTOR_APPROVAL, not_(_past_deadline(now))]


def upcoming_session(now: dt.datetime) -> list[Any]:
    """A confirmed session that has not started yet."""
    return [Session.status == SessionStatus.CONFIRMED, Session.starts_at > now]


async def booking_counts(session: AsyncSession, user_id: UUID, now: dt.datetime) -> dict[str, int]:
    """The caller's four booking counts, one grouped query.

    Each count is scoped to the caller's side of the session in its `FILTER`, so
    a dual-role user's sessions as a mentee never count as their mentoring and
    the reverse. The outer `WHERE` restricts to `LIVE_STATUSES`, the predicate
    of `ix_sessions_mentor_upcoming` and `ix_sessions_mentee_upcoming`, so the
    either-party `OR` can be answered from those two partial indexes.
    """
    mentor, mentee = Session.mentor_id == user_id, Session.mentee_id == user_id
    row = (
        await session.execute(
            select(
                func.count().filter(and_(mentor, *awaiting_mentor(now))).label("mentor_awaiting"),
                func.count().filter(and_(mentor, *upcoming_session(now))).label("mentor_upcoming"),
                func.count().filter(and_(mentee, *awaiting_mentor(now))).label("mentee_awaiting"),
                func.count().filter(and_(mentee, *upcoming_session(now))).label("mentee_upcoming"),
            )
            .select_from(Session)
            .where(or_(mentor, mentee), text(LIVE_STATUSES))
        )
    ).one()
    return {key: int(value) for key, value in row._mapping.items()}
