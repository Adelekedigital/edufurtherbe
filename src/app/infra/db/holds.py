"""Which suggested times still hold a slot (#339, decision 230).

**One definition, four readers.** The slot grid hides a held time from everyone
but its mentee, the booking writer refuses it to anyone else and spends it for
the mentee who books it, the session read reports whether the offer is still
open, and the drain drops a reminder about an offer that is no longer. All of
them ask :func:`active_hold`, so "still held" cannot mean one thing on the grid
and another at the door.

**No sweep.** A hold is active while it is unbooked and ``held_until`` is ahead,
so one that lapses stops counting the instant it does, with nothing to run.

**The exemption is the offer, not the person.** A mentee may book the exact time
offered to them, at the offering it was offered for; every other hold — anyone
else's, or another of their own — is busy to them like any session.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Select, and_, func, literal, not_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.db.booking_rules import effective_break_minutes
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.sessions import SessionTypeBookingConfig
from app.infra.db.models.suggestions import SessionSuggestion

__all__ = [
    "active_hold",
    "held_offer",
    "held_slots",
    "holds_against",
    "lock_mentor_slots",
    "suggestion_reminder_state",
]

#: Namespace for the per-mentor slot lock, beside `CREDIT_LOCK_NAMESPACE` and for
#: its reason: `pg_advisory_xact_lock` has one global keyspace. ``'SLOT'`` as four
#: ASCII bytes.
SLOT_LOCK_NAMESPACE = 0x534C4F54


def active_hold(now: Any) -> list[Any]:
    """A suggestion that still holds its slot. ``now`` may be a value or ``func.now()``."""
    return [
        SessionSuggestion.accepted_session_id.is_(None),
        SessionSuggestion.held_until > now,
    ]


def _the_offer(mentee_id: UUID, session_type_id: UUID) -> Any:
    """The holds open to this mentee at this offering: offered to them, for it."""
    return and_(
        SessionSuggestion.mentee_id == mentee_id,
        SessionSuggestion.session_type_id == session_type_id,
    )


def _held_window(before: Any) -> tuple[Any, Any]:
    """A hold as the grid sees a session: pushed earlier by the asking offering's
    break and later by its own offering's, so the locked re-check below and the
    grid measure one interval."""
    window = func.session_window(SessionSuggestion.starts_at, SessionSuggestion.duration_minutes)
    after = func.make_interval(0, 0, 0, 0, 0, effective_break_minutes())
    return func.lower(window) - before, func.upper(window) + after


def _hold_joins(statement: Select[Any]) -> Select[Any]:
    return (
        statement.select_from(SessionSuggestion)
        .join(MentorProfile, MentorProfile.user_id == SessionSuggestion.mentor_id)
        .outerjoin(
            SessionTypeBookingConfig,
            SessionTypeBookingConfig.session_type_id == SessionSuggestion.session_type_id,
        )
    )


def held_slots(
    mentor_id: UUID,
    span_start: dt.datetime,
    span_end: dt.datetime,
    *,
    now: dt.datetime,
    holds_for: UUID | None,
    session_type_id: UUID,
) -> Select[Any]:
    """A mentor's held times over the span, shaped like ``slot_store._busy``'s rows.

    ``holds_for`` is the mentee asking, and only their offer **at this offering**
    is left open; ``None`` — the public grid, the next-available card, a mentor
    suggesting — counts every hold. Each carries its offering's break, as a
    booked session does, because a hold is a session that has not been written.
    """
    window = func.session_window(SessionSuggestion.starts_at, SessionSuggestion.duration_minutes)
    statement = _hold_joins(
        select(
            func.lower(window).label("start"),
            func.upper(window).label("end"),
            effective_break_minutes().label("break_minutes"),
        )
    ).where(
        SessionSuggestion.mentor_id == mentor_id,
        *active_hold(now),
        window.op("&&")(func.tstzrange(span_start, span_end)),
    )
    if holds_for is not None:
        statement = statement.where(not_(_the_offer(holds_for, session_type_id)))
    return statement


async def held_offer(
    session: AsyncSession,
    *,
    mentor_id: UUID,
    mentee_id: UUID,
    session_type_id: UUID,
    starts_at: dt.datetime,
    now: dt.datetime,
) -> dict[str, Any] | None:
    """The offer this booking takes up, locked, or ``None``. Read under the lock.

    Scoped to the mentee in the query: a time offered to somebody else is never
    theirs to spend.
    """
    row = (
        (
            await session.execute(
                select(SessionSuggestion.id, SessionSuggestion.duration_minutes)
                .where(
                    SessionSuggestion.mentor_id == mentor_id,
                    _the_offer(mentee_id, session_type_id),
                    SessionSuggestion.starts_at == starts_at,
                    *active_hold(now),
                )
                .order_by(SessionSuggestion.created_at)
                .limit(1)
                .with_for_update()
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def holds_against(
    session: AsyncSession,
    mentor_id: UUID,
    *,
    starts_at: dt.datetime,
    duration_minutes: int,
    break_minutes: int,
    now: dt.datetime,
    except_id: UUID | None,
) -> bool:
    """Whether any hold but the one being spent overlaps this booking.

    Measured the way the grid measures it — the hold pushed earlier by this
    offering's break and later by its own — so a booking that waited on the
    lock is refused exactly where the grid would no longer have offered it.
    """
    start, end = _held_window(func.make_interval(0, 0, 0, 0, 0, literal(break_minutes)))
    booked = func.session_window(starts_at, duration_minutes)
    statement = _hold_joins(select(SessionSuggestion.id)).where(
        SessionSuggestion.mentor_id == mentor_id,
        *active_hold(now),
        func.tstzrange(start, end).op("&&")(booked),
    )
    if except_id is not None:
        statement = statement.where(SessionSuggestion.id != except_id)
    return await session.scalar(statement.limit(1)) is not None


async def lock_mentor_slots(session: AsyncSession, mentor_id: UUID) -> None:
    """Serialise the writes that claim a mentor's time against each other.

    **A hold is not a session, so the exclusion constraint cannot see it.** Two
    transactions — a mentor suggesting a time and another mentee booking it —
    would each pass their own check while the other's row was uncommitted. This
    lock is taken by booking *after* its grid read and by suggesting *before*
    its own, so whichever commits second sees the first. Held until the
    transaction ends.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:ns, hashtext(:key))"),
        {"ns": SLOT_LOCK_NAMESPACE, "key": str(mentor_id)},
    )


async def suggestion_reminder_state(
    session: AsyncSession, session_id: UUID, payload: dict[str, Any], now: dt.datetime
) -> Literal["due", "wait", "stale"]:
    """Whether a queued hold reminder is still worth sending, checked at the drain.

    The callback queues it while the offer is open, but the drain runs on its
    own schedule — the offer may be booked, or the hold lapsed, by the time it
    sends. Urging a mentee to book an offer that is gone is the false
    instruction `STILL_DUE` exists to stop.
    """
    del payload
    found = await session.scalar(
        select(SessionSuggestion.id).where(
            SessionSuggestion.session_id == session_id, *active_hold(now)
        )
    )
    return "due" if found is not None else "stale"
