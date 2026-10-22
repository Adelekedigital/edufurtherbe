"""Measured minutes for EduFurther video sessions (Integrations PRD, 2026-10-21).

**After the attendance is settled and committed, never in its way.** Minutes
move a figure on a stats card, not a refund, so a read that fails or runs out
of time leaves the session on its booked length and is tried again next run.
The settlement job also runs this inside its own error boundary, so nothing
here can hold back what comes after it. ``domain.session_minutes`` holds the
arithmetic; this module holds *when* to ask (:func:`_to_measure`) and stores
the result.

**Only completed sessions with a Daily room.** Meet reports no stays and a
migrated session has no room, so neither is read: no provider call is spent on
a session that can only count its booked length.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.attendance import PRESENCE_RECORDS_LAG, PRESENCE_RECORDS_PATIENCE
from app.domain.availability import SESSION_DURATION_MINUTES
from app.domain.enums import SessionStatus
from app.domain.session_minutes import Interval, co_present_minutes
from app.infra.clients.daily_presence import Stay
from app.infra.clients.meetings import RoomsPort, VenueUnavailableError
from app.infra.db.models.sessions import Session
from app.infra.db.predicates import session_ends
from app.infra.db.session_writer.attendance import RECORDS_READ_BUDGET, presence_decides_sql

__all__ = ["measure_minutes"]

logger = logging.getLogger(__name__)


async def _to_measure(
    session: AsyncSession, now: dt.datetime
) -> list[tuple[UUID, str, UUID, UUID]]:
    """Completed Daily sessions not yet measured, over and written (Daily's
    ``PRESENCE_RECORDS_LAG`` past the end) and still inside the patience,
    oldest first so a run the budget cuts short reaches them next time.

    **The one statement of when to ask.** Past the patience a session keeps its
    booked length rather than being read on every run for ever."""
    ends = session_ends()
    longest = dt.timedelta(minutes=SESSION_DURATION_MINUTES[1])
    rows = await session.execute(
        select(Session.id, Session.external_room_id, Session.mentor_id, Session.mentee_id)
        .where(
            Session.status == SessionStatus.COMPLETED,
            presence_decides_sql(),
            Session.measured_minutes.is_(None),
            # Only bounds the scan to `ix_sessions_starts_at`; `ends` decides.
            Session.starts_at > now - PRESENCE_RECORDS_PATIENCE - longest,
            ends <= literal(now - PRESENCE_RECORDS_LAG),
            ends > literal(now - PRESENCE_RECORDS_PATIENCE),
        )
        .order_by(ends)
    )
    return [(r[0], str(r[1]), r[2], r[3]) for r in rows.all()]


def _of(stays: Iterable[Stay], party: UUID) -> list[Interval]:
    """One party's stays. The id is parsed as a UUID, as `observe_presence`
    parses a sighting's, so the two readers match a person the same way; an id
    that is not one of ours matches nobody."""
    found: list[Interval] = []
    for stay in stays:
        try:
            if UUID(stay.user_id) == party:
                found.append((stay.joined, stay.left))
        except ValueError:
            continue
    return found


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

    **Every per-session failure is a per-session skip.** Candidates come oldest
    first, so a session that fails the same way on every run would otherwise
    hold back every newer one for the whole patience. Each write is in its own
    savepoint, so a refused one cannot abort the transaction for the rest.
    """
    measured = 0
    deadline = time.monotonic() + budget.total_seconds()
    for session_id, room, mentor_id, mentee_id in await _to_measure(session, now):
        if time.monotonic() >= deadline:
            break
        try:
            stays = await asyncio.to_thread(rooms.stays, room)
        except NotImplementedError as exc:
            # No provider: no session can be read this run, so stop asking.
            logger.info("minutes not measured, no room provider: %s", exc)
            break
        except VenueUnavailableError as exc:
            # A meeting still going, or a body we cannot read: the booked
            # length stands, and the next run asks again.
            logger.info("minutes not measured for session %s yet: %s", session_id, exc)
            continue
        except Exception:
            logger.exception("minutes not measured for session %s: unexpected failure", session_id)
            continue
        try:
            minutes = co_present_minutes(_of(stays, mentor_id), _of(stays, mentee_id))
            async with session.begin_nested():
                await session.execute(
                    update(Session).where(Session.id == session_id).values(measured_minutes=minutes)
                )
        except Exception:
            logger.exception("minutes not stored for session %s", session_id)
            continue
        measured += 1
    return measured
