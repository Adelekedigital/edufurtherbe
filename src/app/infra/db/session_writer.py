"""Creating a session. The first write to ``sessions`` this project has.

**Legality is asked of :func:`list_slots`, not re-derived here.** Whether an
instant may be booked depends on the offering's scheduling windows or the
mentor's general availability, the exceptions that subtract from either, the
notice window, the duration, and every session already on the calendar. All of
that is one function, and a second implementation of it here would be
non-negotiable #8 in its most expensive form — the copy that drifts silently
offers or refuses the wrong hour, and the two are tested apart so neither test
notices.

It also settles a question that would otherwise be answered twice: a slot the
public endpoint offers is a slot this endpoint accepts, by construction rather
than by agreement.

**The database is still the authority on conflicts.**
``sessions_no_mentor_double_booking`` is an ``EXCLUDE`` over the live statuses,
and the check above cannot replace it: between reading the slots and inserting
the row, another mentee can book the same hour. So the insert is attempted and
its refusal is mapped to a 409. Checking and then trusting the check is the race
the constraint exists to close.

**A refused booking leaves nothing behind.** The idempotency reservation is
written in this same transaction, so a rollback releases it and the client's
retry gets a clean attempt rather than being told forever that a request is in
flight.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.domain.enums import SessionStatus
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.sessions import (
    Session,
    SessionEvent,
    SessionType,
    SessionTypeBookingConfig,
)
from app.infra.db.models.user import User
from app.infra.db.public_visibility import mentor_is_public, session_type_is_live
from app.infra.db.slot_store import list_slots

__all__ = ["DOUBLE_BOOKED", "book_session"]

#: The exclusion constraint's name, which is how a 409 is told from a 500.
#:
#: An insert here can violate three constraints — this one, ``no_self_booking``
#: and ``status_is_known`` — and only this one is the caller's ordinary bad luck.
#: Mapping every ``IntegrityError`` to 409 would report our own bug as the
#: client's conflict, and they would retry forever against a row that can never
#: be written.
DOUBLE_BOOKED = "sessions_no_mentor_double_booking"

#: How far either side of the requested instant to ask ``list_slots`` for.
#:
#: The slot grid is addressed in the **mentor's** calendar days while a booking
#: names a UTC instant, and the two disagree by up to fourteen hours. One day
#: either side covers every zone with room to spare, and the extra days cost a
#: handful of rows that the membership test discards.
SPAN_DAYS = 1


async def _whose(session: AsyncSession, session_type_id: UUID) -> UUID | None:
    """Which mentor an offering belongs to. **Unscoped, and nothing is returned
    from it.**

    Every predicate in ``public_visibility`` is written around a known mentor,
    because every other caller has one in the URL. Booking does not: the request
    names an offering and the mentor is *derived* from it, so the scope has to be
    found before it can be applied. Passing a column into
    ``session_type_is_live`` in place of the mentor id would make its ownership
    clause a tautology — the predicate would still read as scoped and would check
    nothing, which is the failure that file exists to prevent.

    So this reads the id and hands it straight back in as the scope, and its
    result reaches no response: an id that fails the real check below produces
    the same 404 as one that does not exist.
    """
    return (
        await session.execute(
            select(SessionType.mentor_user_id).where(SessionType.id == session_type_id)
        )
    ).scalar_one_or_none()


async def _offering(
    session: AsyncSession, session_type_id: UUID, mentor_id: UUID
) -> dict[str, Any] | None:
    """The offering as a stranger sees it, plus whether booking it needs an answer.

    **``COALESCE`` is the inherit rule, in the one place it is read.** A null on
    the config means *follow the mentor's own setting*; the mentor's column is
    ``NOT NULL``, so the chain always bottoms out — which is what makes the
    nullable boolean legitimate here and was not true of the primary-offering
    cascade it replaced.

    Visibility is the public predicate pair, spread unchanged: a booking is only
    possible where a slot is, and an offering a stranger cannot see is not one a
    stranger may book. Returning ``None`` for all six reasons is the same 404
    ``/slots`` already gives.
    """
    row = (
        (
            await session.execute(
                select(
                    SessionTypeBookingConfig.duration_minutes,
                    func.coalesce(
                        SessionTypeBookingConfig.requires_booking_confirmation,
                        MentorProfile.requires_booking_confirmation,
                    ).label("requires_confirmation"),
                )
                .select_from(SessionType)
                .join(
                    SessionTypeBookingConfig,
                    SessionTypeBookingConfig.session_type_id == SessionType.id,
                )
                .join(MentorProfile, MentorProfile.user_id == SessionType.mentor_user_id)
                .join(User, User.id == SessionType.mentor_user_id)
                .where(
                    SessionType.id == session_type_id,
                    *session_type_is_live(mentor_id),
                    *mentor_is_public(),
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row else None


async def book_session(
    session: AsyncSession,
    mentee_id: UUID,
    payload: dict[str, Any],
    *,
    now: dt.datetime,
) -> UUID:
    """Book ``starts_at`` on an offering, and return the new session's id.

    Raises :class:`NotFoundError` when the offering is not publicly bookable —
    six reasons, one answer, the same conflation ``/slots`` makes.
    :class:`ValidationError` when the instant is not one the mentor offers, or
    when the mentee is the mentor. :class:`ConflictError` when the mentor was
    booked into that hour between the check and the write.

    **Does not commit.** The caller owns the transaction, because the
    idempotency reservation and this row are one unit — see the module
    docstring.
    """
    starts_at: dt.datetime = payload["starts_at"]
    mentor_id = await _whose(session, payload["session_type_id"])
    offering = (
        await _offering(session, payload["session_type_id"], mentor_id) if mentor_id else None
    )
    if offering is None or mentor_id is None:
        raise NotFoundError("no such bookable session type")

    if mentor_id == mentee_id:
        # `no_self_booking` would catch this, and a CHECK violation is a 500.
        # Refused here so the mentor who is also a mentee gets told why — dual
        # roles are free by design, so this is a reachable mistake rather than a
        # malformed request.
        raise ValidationError("you cannot book your own session type")

    # **The one legality check, and it is a membership test against the public
    # grid.** Asking for a span in the mentor's days and comparing instants
    # keeps the timezone question where it is already answered.
    day = starts_at.astimezone(dt.UTC).date()
    slots = await list_slots(
        session,
        mentor_id,
        payload["session_type_id"],
        start=day - dt.timedelta(days=SPAN_DAYS),
        end=day + dt.timedelta(days=SPAN_DAYS + 1),
        now=now,
    )
    if not slots or not any(slot.start == starts_at for slot in slots):
        # Deliberately not distinguished. "Too soon", "outside your hours",
        # "already taken" and "that is not on the grid" are all *this instant is
        # not offered*, and a client's only correct response to each is to
        # re-read `/slots` — which the message says.
        raise ValidationError("that time is not available — re-read the mentor's slots")

    status = (
        SessionStatus.PENDING_MENTOR_APPROVAL
        if offering["requires_confirmation"]
        else SessionStatus.CONFIRMED
    )

    try:
        session_id = (
            await session.execute(
                insert(Session)
                .values(
                    mentor_id=mentor_id,
                    mentee_id=mentee_id,
                    created_by=mentee_id,
                    session_type_id=payload["session_type_id"],
                    status=status,
                    starts_at=starts_at,
                    # **Snapshotted, never read live from the config.** Settled
                    # decision #10 gives the reason for a mentor's rate and it
                    # is the same one here: a later edit to the offering must
                    # not silently rewrite what was agreed.
                    duration_minutes=offering["duration_minutes"],
                    topic=payload.get("topic"),
                    booking_message=payload.get("booking_message"),
                )
                .returning(Session.id)
            )
        ).scalar_one()
    except IntegrityError as exc:
        if DOUBLE_BOOKED not in str(exc.orig):
            raise
        # Rolled back here rather than left to the caller: the transaction is
        # already aborted, so every later statement in it would fail with
        # `InFailedSQLTransaction` and bury this cause under that one.
        await session.rollback()
        raise ConflictError("that time was taken while you were booking it") from exc

    # **The event is written by the same transaction as the status**, per the
    # model's own note: a trigger projecting one onto the other would be a
    # second mechanism for one fact.
    #
    # `from_status` is null because there is no prior state. That is the
    # creation event's signature and the reason the column is nullable.
    await session.execute(
        insert(SessionEvent).values(
            session_id=session_id,
            from_status=None,
            to_status=status,
            actor_id=mentee_id,
            actor_type="user",
        )
    )
    return session_id
