"""Measured minutes for EduFurther video sessions (Integrations PRD, 2026-10-21).

**After the attendance is settled and committed, never in its way.** Minutes
move a figure on a stats card, not a refund, so a read that fails or runs out
of time leaves the session on its booked length and is tried again next run;
nothing here holds a settlement back. ``domain.session_minutes`` holds the
arithmetic and the timing; this module picks the sessions and stores the result.

**Only completed sessions with a Daily room.** Meet reports no stays and a
migrated session has no room, so neither is read: no provider call is spent on
a session that can only count its booked length.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from typing import Any
from uuid import UUID

from sqlalchemy import func, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.attendance import PRESENCE_RECORDS_LAG, PRESENCE_RECORDS_PATIENCE
from app.domain.enums import SessionStatus
from app.domain.session_minutes import co_present_minutes
from app.infra.clients.meetings import RoomsPort, VenueUnavailableError
from app.infra.db.models.sessions import Session
from app.infra.db.session_writer.attendance import RECORDS_READ_BUDGET, presence_decides_sql

__all__ = ["measure_minutes"]

logger = logging.getLogger(__name__)


def _ends_sql() -> Any:
    """A session's end: the upper bound of ``session_window()``, the database's
    one definition, which the exclusion constraint and the slots read too."""
    return func.upper(func.session_window(Session.starts_at, Session.duration_minutes))


async def _to_measure(
    session: AsyncSession, now: dt.datetime
) -> list[tuple[UUID, str, UUID, UUID]]:
    """Completed Daily sessions not yet measured, over and written (Daily's
    ``PRESENCE_RECORDS_LAG`` past the end) and still inside the patience,
    oldest first so a run the budget cuts short reaches them next time.

    **The one statement of when to ask.** Past the patience a session keeps its
    booked length rather than being read on every run for ever."""
    ends = _ends_sql()
    rows = await session.execute(
        select(Session.id, Session.external_room_id, Session.mentor_id, Session.mentee_id)
        .where(
            Session.status == SessionStatus.COMPLETED,
            presence_decides_sql(),
            Session.measured_minutes.is_(None),
            ends <= literal(now - PRESENCE_RECORDS_LAG),
            ends > literal(now - PRESENCE_RECORDS_PATIENCE),
        )
        .order_by(ends)
    )
    return [(r[0], str(r[1]), r[2], r[3]) for r in rows.all()]


async def measure_minutes(
    session: AsyncSession,
    *,
    now: dt.datetime,
    rooms: RoomsPort,
    budget: dt.timedelta = RECORDS_READ_BUDGET,
) -> int:
    """Store how long each due session's mentor and mentee were in the room
    together. Returns how many were measured. Does not commit.

    The provider's client is blocking, so each read runs in a thread, under the
    same read budget the presence check uses.
    """
    measured = 0
    deadline = time.monotonic() + budget.total_seconds()
    for session_id, room, mentor_id, mentee_id in await _to_measure(session, now):
        if time.monotonic() >= deadline:
            break
        try:
            stays = await asyncio.to_thread(rooms.stays, room)
        except (VenueUnavailableError, NotImplementedError) as exc:
            # A meeting still going, a body we cannot read, or no provider: the
            # booked length stands, and the next run asks again.
            logger.info("minutes not measured for session %s yet: %s", session_id, exc)
            continue
        minutes = co_present_minutes(
            [(s.joined, s.left) for s in stays if s.user_id == str(mentor_id)],
            [(s.joined, s.left) for s in stays if s.user_id == str(mentee_id)],
        )
        await session.execute(
            update(Session).where(Session.id == session_id).values(measured_minutes=minutes)
        )
        measured += 1
    return measured
