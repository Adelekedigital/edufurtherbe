"""What follows a session becoming real, or ending — in one place.

**Every way in calls these, never the steps.** Confirmation happens at booking
(an offering that confirms itself) and at `/accept`; an ending at decline,
withdraw, cancel and the expiry sweep. Each of those used to repeat the steps,
and a missed one is how `release_meeting` came to be needed after the fact.

**A convention, not a guarantee** (decision 240): `book_session` and
`transition` change the status and their callers run these, so a new way in —
an admin cancel, a reschedule — must call them too. The steps themselves are not
exported from the package, so reaching past these takes a deliberate import.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import SessionStatus
from app.infra.clients.meetings import CalendarPort, RoomsPort
from app.infra.clients.scheduler import SchedulerPort
from app.infra.db.models.sessions import Session
from app.infra.db.session_writer.meetings import provision_meeting, release_meeting
from app.infra.db.session_writer.reminders import schedule_session_reminders


@dataclass(frozen=True, slots=True)
class FollowUps:
    """The adapters and addresses the follow-up steps need, wired once."""

    rooms: RoomsPort
    calendar: CalendarPort
    scheduler: SchedulerPort | None
    #: Where QStash calls back; ``None`` when this service cannot say, and then
    #: no reminder is scheduled.
    callback_url: str | None
    #: The front end's origin, for the link in the calendar invite.
    app_base_url: str


async def on_confirmed(
    session: AsyncSession, session_id: UUID, follow_ups: FollowUps, *, now: dt.datetime
) -> None:
    """Give a confirmed session its venue and invite, then its reminders.

    **A no-op unless the session is confirmed**, so a booking that waits for
    the mentor can call it too: that request gets both at `/accept`. Telling
    both parties their session is tomorrow, for a request nobody has accepted,
    is the bug the check exists to prevent. Does not commit.
    """
    starts_at = await session.scalar(
        select(Session.starts_at).where(
            Session.id == session_id, Session.status == SessionStatus.CONFIRMED
        )
    )
    if starts_at is None:
        return
    # The venue first: if it fails in a way nothing catches, the booking rolls
    # back before any reminder reaches QStash for a session that never was.
    await provision_meeting(
        session,
        session_id,
        rooms=follow_ups.rooms,
        calendar=follow_ups.calendar,
        app_base_url=follow_ups.app_base_url,
    )
    # Off the event loop (#370): four QStash round trips.
    await asyncio.to_thread(
        schedule_session_reminders,
        session_id,
        starts_at,
        scheduler=follow_ups.scheduler,
        callback_url=follow_ups.callback_url or "",
        now=now,
    )


async def on_ended(session: AsyncSession, session_id: UUID, *, calendar: CalendarPort) -> None:
    """An ended session leaves no live event in either party's calendar.
    Does not commit."""
    await release_meeting(session, session_id, calendar=calendar)
