"""Which suggested times still hold a slot (#339, decision 230).

**One definition, three readers.** The slot grid hides a held time from everyone
but its mentee, the booking writer refuses it to anyone else and attaches it to
the mentee who books it, and the session read reports whether the offer is still
open. All three ask :func:`active_hold`, so "still held" cannot mean one thing on
the grid and another at the door.

**No sweep.** A hold is active while it is unbooked and ``held_until`` is ahead,
so one that lapses stops counting the instant it does, with nothing to run.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.db.booking_rules import effective_break_minutes
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.sessions import SessionTypeBookingConfig
from app.infra.db.models.suggestions import SessionSuggestion

__all__ = ["active_hold", "held_slots", "holds_against", "lock_mentor_slots"]

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


def held_slots(
    mentor_id: UUID,
    span_start: dt.datetime,
    span_end: dt.datetime,
    *,
    now: dt.datetime,
    holds_for: UUID | None,
) -> Select[Any]:
    """A mentor's held times over the span, shaped like ``slot_store._busy``'s rows.

    **Except the viewer's own.** ``holds_for`` is the mentee asking: a time held
    *for* them is open to them and closed to everyone else. ``None`` — the public
    grid, the next-available card, a mentor suggesting — counts every hold.

    Each hold carries its offering's break, as a booked session does, because a
    hold is a session that has not been written yet.
    """
    window = func.session_window(SessionSuggestion.starts_at, SessionSuggestion.duration_minutes)
    statement = (
        select(
            func.lower(window).label("start"),
            func.upper(window).label("end"),
            effective_break_minutes().label("break_minutes"),
        )
        .select_from(SessionSuggestion)
        .join(MentorProfile, MentorProfile.user_id == SessionSuggestion.mentor_id)
        .outerjoin(
            SessionTypeBookingConfig,
            SessionTypeBookingConfig.session_type_id == SessionSuggestion.session_type_id,
        )
        .where(
            SessionSuggestion.mentor_id == mentor_id,
            *active_hold(now),
            window.op("&&")(func.tstzrange(span_start, span_end)),
        )
    )
    if holds_for is not None:
        statement = statement.where(SessionSuggestion.mentee_id != holds_for)
    return statement


async def holds_against(
    session: AsyncSession,
    mentor_id: UUID,
    mentee_id: UUID,
    *,
    starts_at: dt.datetime,
    duration_minutes: int,
    now: dt.datetime,
) -> bool:
    """Whether another mentee's hold overlaps this booking. Read under the lock."""
    window = func.session_window(SessionSuggestion.starts_at, SessionSuggestion.duration_minutes)
    found = await session.scalar(
        select(SessionSuggestion.id)
        .where(
            SessionSuggestion.mentor_id == mentor_id,
            SessionSuggestion.mentee_id != mentee_id,
            *active_hold(now),
            window.op("&&")(func.session_window(starts_at, duration_minutes)),
        )
        .limit(1)
    )
    return found is not None


async def lock_mentor_slots(session: AsyncSession, mentor_id: UUID) -> None:
    """Serialise the writes that claim a mentor's time against each other.

    **A hold is not a session, so the exclusion constraint cannot see it.** Two
    transactions — a mentor suggesting a time and another mentee booking it —
    would each pass their own check while the other's row was uncommitted. This
    lock is taken by both *after* their slot check and *before* the last read
    (booking) or the whole check (suggesting), so whichever commits second sees
    the first. Held until the transaction ends.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:ns, hashtext(:key))"),
        {"ns": SLOT_LOCK_NAMESPACE, "key": str(mentor_id)},
    )
