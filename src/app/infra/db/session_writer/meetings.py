"""The room a confirmed session happens in: provisioning it, and releasing
it when the session will not happen.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.attendance import (
    door_window,
)
from app.domain.enums import (
    MeetingProvider,
    SessionStatus,
)
from app.domain.meetings import plan_for
from app.infra.clients.meetings import VenueUnavailableError, room_name
from app.infra.db.models.sessions import (
    Session,
)
from app.infra.db.models.user import User
from app.infra.db.session_type_store import resolve_venue

logger = logging.getLogger(__name__)


async def provision_meeting(
    session: AsyncSession,
    session_id: UUID,
    *,
    rooms: Any,
    calendar: Any,
) -> None:
    """Give a newly confirmed session somewhere to meet. Does not commit.

    **Called at confirmation, which is two places rather than one:** booking, for
    an offering that auto-confirms, and ``/accept`` for one that does not. The
    model has always said the link is generated per session at confirmation —
    *"a static personal room means back-to-back sessions share it and an early
    joiner walks into the previous one"* — and until now nothing generated it.

    **The provider decides two independent things**, and `plan_for` holds the
    table because a reader who assumes *needs a room* and *wants a conference*
    are opposites gets Meet right and Daily wrong. Meet's link is a property of
    the calendar event; Daily's room is its own object created first; a custom
    venue is a URL that already exists.

    **A failure here does not fail the booking.** The session exists, the slot is
    held, and a link can be minted later — where refusing the booking because a
    third party was slow loses something that cannot be recovered. So the room
    and calendar calls are attempted and their absence is left as a null column
    rather than raised, which is also exactly what happens today with the null
    adapters wired.
    """
    row = (
        (
            await session.execute(
                select(
                    Session.status,
                    Session.session_type_id,
                    Session.starts_at,
                    Session.duration_minutes,
                    Session.meeting_url,
                    Session.mentee_id,
                ).where(Session.id == session_id)
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None or row["session_type_id"] is None:
        return
    # **The guard lives here, not at the two call sites.** Booking calls this
    # for every session and only some of them confirm, so a check at the caller
    # is a check somebody has to remember twice — and the cost of forgetting is
    # a room minted for a request that may be declined, which on a metered
    # provider is a room somebody pays for. Caught by a test asserting a pending
    # request gets nothing, which the first version of this failed.
    if SessionStatus(row["status"]) is not SessionStatus.CONFIRMED:
        return

    mentee_email = (
        await session.execute(select(User.email).where(User.id == row["mentee_id"]))
    ).scalar_one_or_none()

    resolved = await resolve_venue(session, row["session_type_id"])
    if resolved is None:  # pragma: no cover - the offering was just booked
        return
    provider, custom_url = resolved
    plan = plan_for(provider)

    meeting_url: str | None = custom_url if plan.reuses_a_static_room else row["meeting_url"]
    external_room_id: str | None = None
    external_event_id: str | None = None

    # The room is open exactly as long as the door: from the join window opening
    # to the session's end. One definition, so a door is never issued onto a
    # room that has closed or not yet opened.
    opens, closes = door_window(row["starts_at"], int(row["duration_minutes"]))
    try:
        if plan.needs_room:
            room = rooms.create(
                name=room_name(str(session_id), provider),
                opens_at=opens,
                closes_at=closes,
            )
            meeting_url, external_room_id = room.url, room.external_id
    except (VenueUnavailableError, NotImplementedError) as exc:
        logger.info("no room for session %s: %s", session_id, exc)

    try:
        event = calendar.create_event(
            organiser_id=str(session_id),
            # **The mentee is the guest.** The platform account organises and
            # the mentor is told through the platform, so an empty address here
            # would create an event nobody outside this service ever sees.
            attendee_email=str(mentee_email or ""),
            starts_at=row["starts_at"],
            duration_minutes=int(row["duration_minutes"]),
            summary="EduFurther session",
            # **Only Meet, and this is the line that matters.** Asking for a
            # conference on a session held in Daily puts a second link on the
            # event, and the invitee clicks whichever the client renders first.
            wants_conference=plan.wants_conference,
            meeting_url=meeting_url,
        )
    except (VenueUnavailableError, NotImplementedError) as exc:
        logger.info("no calendar event for session %s: %s", session_id, exc)
        event = None

    if event is not None:
        external_event_id = event.external_id
        # Meet's link arrives by this path and no other, so it is taken only
        # when the event was asked for one — otherwise the URL we already had
        # stands.
        meeting_url = event.meeting_url or meeting_url

    await session.execute(
        update(Session)
        .where(Session.id == session_id)
        .values(
            meeting_provider=MeetingProvider(str(provider)),
            meeting_url=meeting_url,
            external_room_id=external_room_id,
            external_calendar_event_id=external_event_id,
        )
    )


async def release_meeting(session: AsyncSession, session_id: UUID, *, calendar: Any) -> None:
    """Remove the calendar event a session no longer needs. Does not commit.

    **The partner fix to `provision_meeting`, and it was missing.**
    `external_calendar_event_id` was written by provisioning and read by
    nobody — harmless while no event was ever created, and a live defect the
    moment one is: a cancelled session would leave a meeting sitting in both
    parties' calendars forever, with an invitation nobody withdrew.

    Called from every path that ends a session before it happens — declined,
    withdrawn, cancelled, expired — but **not** from `completed` or `no_show`,
    where the session did take place or its time has passed and the event is a
    true record of what was scheduled.

    **A failure does not fail the transition**, for the reason every venue call
    in this module gives: the session is already off, and refusing to record
    that because a third party was slow would leave the two facts disagreeing.
    A stale event is recoverable by hand; a session that is cancelled in Google
    and confirmed here is not.
    """
    external_id = (
        await session.execute(
            select(Session.external_calendar_event_id).where(Session.id == session_id)
        )
    ).scalar_one_or_none()
    if not external_id:
        return

    try:
        calendar.cancel_event(str(external_id))
    except (VenueUnavailableError, NotImplementedError) as exc:
        logger.info("calendar event for session %s not removed: %s", session_id, exc)
        return

    # Cleared only on success, so a failed removal leaves the id in place and a
    # later run can try again — where clearing it regardless would lose the only
    # handle on an event still in somebody's calendar.
    await session.execute(
        update(Session).where(Session.id == session_id).values(external_calendar_event_id=None)
    )
