"""Which suggested times still hold a slot (#339, decision 230).

**One definition, four readers.** The slot grid hides a held time from everyone
but its mentee, the booking writer refuses it to anyone else and spends it for
the mentee who books it, the session read reports whether the offer is still
open, and the drain drops a reminder about an offer that is no longer. All of
them ask :func:`active_hold`, so "still held" cannot mean one thing on the grid
and another at the door.

**No sweep.** A hold is active while it is unbooked and ``held_until`` is ahead,
so one that lapses stops counting the instant it does, with nothing to run.

**Taking up an offer is wider than the hold** (owner, 2026-10-13): a decline's
offer is approved in advance and stays bookable by its mentee after the hold
lapses, so booking reads :func:`takeable`, not :func:`active_hold`. The hold
still decides only who else may see and book the time.

**The exemption is the offer, not the person.** A mentee may book the exact time
offered to them, at the offering it was offered for; every other hold — anyone
else's, or another of their own — is busy to them like any session.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Select, and_, exists, func, literal, not_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.suggestions import APPROVED_IN_ADVANCE_AFTER
from app.infra.db.booking_rules import effective_break_minutes
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.sessions import Session, SessionTypeBookingConfig
from app.infra.db.models.suggestions import SessionSuggestion

__all__ = [
    "active_hold",
    "approved_in_advance",
    "held_slots",
    "holds_against",
    "lock_mentor_slots",
    "open_offer",
    "suggestion_reminder_state",
    "takeable",
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


def approved_in_advance() -> Any:
    """Whether an offer carries the mentor's approval: it was made on ending a
    session in :data:`APPROVED_IN_ADVANCE_AFTER`, a decline (owner, 2026-10-13).
    Read from the original's status, so nothing new is stored."""
    # Correlated on the offer's own session: one primary-key probe per row.
    return exists().where(
        Session.id == SessionSuggestion.session_id,
        Session.status.in_(APPROVED_IN_ADVANCE_AFTER),
    )


def takeable(now: Any) -> list[Any]:
    """An offer its mentee may still book: unbooked, and **held or approved in
    advance**. The hold hides the time from others for two hours; an approved
    offer stays bookable after it, while the time is still free on the grid."""
    return [
        SessionSuggestion.accepted_session_id.is_(None),
        or_(SessionSuggestion.held_until > now, approved_in_advance()),
    ]


def _the_offer(mentee_id: UUID, session_type_id: UUID, holds: Any = SessionSuggestion) -> Any:
    """The holds open to this mentee at this offering: offered to them, for it."""
    return and_(holds.mentee_id == mentee_id, holds.session_type_id == session_type_id)


def _active_holds(mentor_id: UUID, now: Any) -> Any:
    """**Every reader's hold, written once**: a mentor's active holds, each with
    its window and its own offering's break — the grid subtracts these and the
    locked re-check refuses on them, so the two cannot measure differently."""
    window = func.session_window(SessionSuggestion.starts_at, SessionSuggestion.duration_minutes)
    return (
        select(
            SessionSuggestion.id,
            SessionSuggestion.mentee_id,
            SessionSuggestion.session_type_id,
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
        .where(SessionSuggestion.mentor_id == mentor_id, *active_hold(now))
        .subquery("holds")
    )


def _minutes(value: Any) -> Any:
    return func.make_interval(0, 0, 0, 0, 0, value)


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
    holds = _active_holds(mentor_id, now)
    statement = select(holds.c.start, holds.c.end, holds.c.break_minutes).where(
        func.tstzrange(holds.c.start, holds.c.end).op("&&")(func.tstzrange(span_start, span_end))
    )
    if holds_for is not None:
        statement = statement.where(not_(_the_offer(holds_for, session_type_id, holds.c)))
    return statement


async def open_offer(
    session: AsyncSession,
    *,
    mentor_id: UUID,
    mentee_id: UUID,
    session_type_id: UUID,
    starts_at: dt.datetime,
    now: dt.datetime,
    lock: bool = True,
) -> dict[str, Any] | None:
    """The offer this booking takes up, or ``None``. Locked unless ``lock=False``.

    Scoped to the mentee in the query: a time offered to somebody else is never
    theirs to spend. Read once unlocked, before the grid, for where the notice
    floor is measured from; and again under the lock, which is the read that
    decides.

    **Every offer of this time to this mentee is taken up together**, since
    one mentor can offer the same hour twice (a decline's, then a cancel's). The
    returned offer is a held one if any, so the hold check skips it; ``ids``
    is all of them, which booking spends at once; ``approved`` is whether any
    was approved in advance; ``held`` is whether the returned one still holds,
    which is all the notice exemption may lean on.
    """
    statement = (
        select(
            SessionSuggestion.id,
            SessionSuggestion.duration_minutes,
            SessionSuggestion.created_at,
            (SessionSuggestion.held_until > now).label("held"),
            approved_in_advance().label("approved"),
        )
        .where(
            SessionSuggestion.mentor_id == mentor_id,
            _the_offer(mentee_id, session_type_id),
            SessionSuggestion.starts_at == starts_at,
            *takeable(now),
        )
        .order_by(SessionSuggestion.created_at)
    )
    if lock:
        # The offers' rows only: the original sessions are read, not claimed.
        statement = statement.with_for_update(of=SessionSuggestion)
    rows = [dict(row) for row in (await session.execute(statement)).mappings()]
    if not rows:
        return None
    offer = next((row for row in rows if row["held"]), rows[0])
    return {
        **offer,
        "ids": tuple(row["id"] for row in rows),
        "approved": any(row["approved"] for row in rows),
    }


async def holds_against(
    session: AsyncSession,
    mentor_id: UUID,
    *,
    starts_at: dt.datetime,
    duration_minutes: int,
    break_minutes: int,
    now: dt.datetime,
    except_ids: tuple[UUID, ...] = (),
) -> bool:
    """Whether any hold but the ones being spent overlaps this booking.

    Measured exactly as the grid measures it — the hold pushed earlier by this
    offering's break and later by its own, from the same `_active_holds` — so a
    booking that waited on the lock is refused exactly where the grid would no
    longer have offered it.
    """
    holds = _active_holds(mentor_id, now)
    statement = select(holds.c.id).where(
        func.tstzrange(
            holds.c.start - _minutes(literal(break_minutes)),
            holds.c.end + _minutes(holds.c.break_minutes),
        ).op("&&")(func.session_window(starts_at, duration_minutes))
    )
    if except_ids:
        statement = statement.where(holds.c.id.not_in(except_ids))
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
    """Whether a queued suggestion or its reminder is still worth sending, at the drain.

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
