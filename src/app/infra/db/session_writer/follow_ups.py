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
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.availability import SESSION_DURATION_MINUTES
from app.domain.enums import ConferencingProvider, MeetingProvider, SessionStatus
from app.infra.clients.meetings import (
    TIMEOUT,
    CalendarPort,
    NullCalendar,
    RoomsPort,
    room_name,
)
from app.infra.clients.scheduler import SchedulerPort
from app.infra.db.models.sessions import FREES_THE_HOUR, Session
from app.infra.db.predicates import session_ends
from app.infra.db.session_writer.meetings import (
    ReleaseOutcome,
    provision_meeting,
    release_meeting,
)
from app.infra.db.session_writer.reminders import schedule_session_reminders

logger = logging.getLogger(__name__)


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


def _migrated_unconfirmed(now: dt.datetime) -> tuple[ColumnElement[bool], ...]:
    """A loaded session the new app has not confirmed: confirmed, from the
    legacy load, with an offering, still ahead, and no calendar event."""
    return (
        Session.status == SessionStatus.CONFIRMED,
        Session.legacy_bubble_id.is_not(None),
        Session.session_type_id.is_not(None),
        Session.external_calendar_event_id.is_(None),
        Session.starts_at > now,
    )


def _no_room_of_ours(session_id: UUID, room: str | None) -> bool:
    """The room column holds nothing, or the legacy app's token, rather than
    the room `provision_meeting` names for this session."""
    return room != room_name(str(session_id), ConferencingProvider.DAILY)


async def migrated_to_confirm(
    session: AsyncSession, *, now: dt.datetime
) -> list[tuple[UUID, dt.datetime]]:
    """Upcoming confirmed sessions the legacy load brought in that the new app
    has never confirmed: ``(id, starts_at)``, soonest first (decision 366).

    A load writes the row but runs none of `on_confirmed`, so such a session has
    no room of its own, no invite and no reminders. **Listed while it has no
    calendar event and no room of ours.** A session whose room was made but
    whose invite failed is not listed again, because a second room under the
    same name is refused; `confirmation_gaps` reports it instead. One where both
    failed is listed again and a rerun is safe (decision 162's reminder index
    sends each reminder once).
    """
    rows = await session.execute(
        select(Session.id, Session.starts_at, Session.external_room_id)
        .where(*_migrated_unconfirmed(now))
        .order_by(Session.starts_at, Session.id)
    )
    return [
        (row.id, row.starts_at) for row in rows if _no_room_of_ours(row.id, row.external_room_id)
    ]


async def clear_legacy_venue(session: AsyncSession, session_id: UUID, *, now: dt.datetime) -> None:
    """Drop the venue the load carried, before `on_confirmed` gives one.

    The legacy value is a mentor's personal room reused across sessions, and
    `provision_meeting` keeps a stored link for a venue that makes no room
    (Meet) or when the room call fails, so left in place it would be handed out
    for good. Scoped to a session still unconfirmed here. Does not commit."""
    row = await session.execute(
        select(Session.external_room_id).where(
            Session.id == session_id, *_migrated_unconfirmed(now)
        )
    )
    found = row.one_or_none()
    if found is None or not _no_room_of_ours(session_id, found.external_room_id):
        return
    await session.execute(
        update(Session)
        .where(Session.id == session_id)
        .values(meeting_provider=None, meeting_url=None, external_room_id=None)
    )


async def confirmation_gaps(
    session: AsyncSession, session_ids: list[UUID]
) -> list[tuple[UUID, str]]:
    """What a confirmation left undone, per session: no calendar invite, or an
    EduFurther video session with no room. Empty when every one is complete."""
    rows = await session.execute(
        select(
            Session.id,
            Session.meeting_provider,
            Session.external_room_id,
            Session.external_calendar_event_id,
        ).where(Session.id.in_(session_ids))
    )
    gaps = []
    for row in rows:
        if row.external_calendar_event_id is None:
            gaps.append((row.id, "no calendar invite"))
        if row.meeting_provider == MeetingProvider.DAILY and row.external_room_id is None:
            gaps.append((row.id, "no video room"))
    return sorted(gaps, key=lambda gap: (str(gap[0]), gap[1]))


async def on_ended(
    session: AsyncSession, session_id: UUID, *, calendar: CalendarPort
) -> ReleaseOutcome:
    """An ended session leaves no live event in either party's calendar.
    Does not commit."""
    return await release_meeting(session, session_id, calendar=calendar)


#: How many ended sessions one settlement run retries at most (#423).
RELEASE_RETRIES_PER_RUN = 5

#: The budget a row needs before it is started: Google's read timeout twice,
#: for the call and the retry a stale token earns it.
RELEASE_ROW_ALLOWANCE_SECONDS = 2 * float(TIMEOUT.read or 0.0)


async def _unreleased(
    session: AsyncSession, *, now: dt.datetime, untouched_since: dt.datetime, limit: int
) -> list[UUID]:
    """Ended sessions that kept their event and have not yet run their course,
    in a random order.

    **Ended is `FREES_THE_HOUR`**: exactly the statuses whose transitions call
    `on_ended`, and not `completed` or `no_show`, whose event is a true record.

    **Until the session would have ended.** The invite still tells both parties
    the hour is on, even after the start; once the session's window has closed
    nobody can turn up, and deleting the event would email both a cancellation
    of something already over (every migrated ended session with an event id
    would get one).

    **Untouched since the run began**, so a row this run's own ending just
    tried is not tried twice.

    **Random, not soonest first.** With a fixed bound, events Google always
    refuses would hold every slot of an ordered queue until their sessions
    ended; drawn at random, every candidate gets a turn.
    """
    ends_at = session_ends()
    ids = await session.scalars(
        select(Session.id)
        .where(
            Session.status.in_(FREES_THE_HOUR),
            Session.external_calendar_event_id.is_not(None),
            Session.updated_at < untouched_since,
            # Only bounds the scan to `ix_sessions_starts_at`; `ends_at` decides.
            Session.starts_at > now - dt.timedelta(minutes=SESSION_DURATION_MINUTES[1]),
            ends_at > now,
        )
        .order_by(func.random())
        .limit(limit)
    )
    return list(ids)


async def retry_releases(
    session: AsyncSession,
    *,
    now: dt.datetime,
    calendar: CalendarPort,
    deadline: float,
    untouched_since: dt.datetime,
    clock: Callable[[], float] = time.monotonic,
    dry_run: bool = False,
    limit: int = RELEASE_RETRIES_PER_RUN,
) -> int:
    """Retry the calendar release an ending could not finish (#423).

    `release_meeting` keeps the event id when Google refuses so that a later
    run can try again; this is that run. Each session goes through `on_ended`,
    the one way an ending releases its event (decision 240), and is **committed
    on its own**: a refusal keeps that row's id and the next row still runs.
    Returns how many events were removed, or on a dry run how many would be
    tried, calling nothing.

    **Paced by `deadline`**, a reading of `clock`: no row is started without
    `RELEASE_ROW_ALLOWANCE_SECONDS` left, so the retry cannot run its job out.

    **A dead platform grant stops the run**, with one warning: every other row
    would fail the same way, and each would keep its id regardless.

    **Nothing to call, nothing cleared.** `NullCalendar` removes nothing and
    reports success, so clearing ids on it would lose the only handle on events
    a configured calendar could still remove.
    """
    if isinstance(calendar, NullCalendar):
        return 0
    pending = await _unreleased(session, now=now, untouched_since=untouched_since, limit=limit)
    if dry_run:
        return len(pending)
    released = 0
    for session_id in pending:
        if clock() + RELEASE_ROW_ALLOWANCE_SECONDS > deadline:
            logger.info("calendar release retries stopped at the job's time budget")
            break
        outcome = await on_ended(session, session_id, calendar=calendar)
        await session.commit()
        if outcome is ReleaseOutcome.REVOKED:
            logger.warning("platform calendar access revoked; calendar releases paused")
            break
        released += outcome is ReleaseOutcome.RELEASED
    return released
