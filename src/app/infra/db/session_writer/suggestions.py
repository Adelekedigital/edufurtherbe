"""Suggesting another time when declining or cancelling, and what follows (#339).

Owner decisions of 2026-10-03 (settled decision 230), in the order they act:

1. **The original ends as it would have.** Declining or cancelling runs the
   ordinary transition — status, event, refund by decision 229 — and the
   suggestion is written after it, **in the same transaction**: a decline that
   lands without the suggestion its mentor sent, or a suggestion for a session
   that was never declined, are both states a retry could not repair.
2. **One time, held two hours for that mentee.** The time must be one the slot
   grid offers right now — the same membership test booking makes — and while it
   is held, the grid shows it to nobody else and booking refuses it to anybody
   else (``infra/db/holds.py``).
3. **One email, not two.** The mentee is told in a single message that the
   session is off *and* what was offered instead, rather than a decline followed
   seconds later by a suggestion.
4. **A nudge thirty minutes before the hold lapses**, scheduled now and
   re-checked when it fires, like every other reminder here: nothing is ever
   unscheduled, and a nudge for an offer already booked or lapsed does nothing.
5. **Booking it is ordinary booking** — `POST /sessions` at the suggested time.
   The booking writer finds the offer under its lock, books it at the length
   it was offered at, and marks it spent (`holds.held_offer`).
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationError
from app.domain.availability import BookingWindow
from app.domain.notifications import Notification, recipients
from app.domain.suggestions import (
    SUGGESTION_REMINDER_KIND,
    held_until,
    suggestion_reminder_at,
)
from app.infra.clients.scheduler import SchedulerError
from app.infra.db.holds import active_hold, lock_mentor_slots
from app.infra.db.models.sessions import Session
from app.infra.db.models.suggestions import SessionSuggestion
from app.infra.db.outbox import enqueue
from app.infra.db.slot_store import list_slots

logger = logging.getLogger(__name__)

__all__ = ["remind_suggestion", "suggest_time"]

#: Where a refused suggestion points, so a client marks the right field.
POINTER = "/suggested_starts_at"

#: How far either side of the suggested instant to ask the grid for, for the
#: reason booking's `SPAN_DAYS` gives: the grid is addressed in the mentor's days.
SPAN_DAYS = 1


def _refuse(message: str) -> ValidationError:
    return ValidationError(message, field_errors=((POINTER, message),))


async def suggest_time(
    session: AsyncSession,
    session_id: UUID,
    actor_id: UUID,
    starts_at: dt.datetime,
    *,
    ended_as: str,
    reason_text: str | None,
    now: dt.datetime,
    window: BookingWindow,
    external_busy: Any = None,
    scheduler: Any = None,
    callback_url: str | None = None,
) -> UUID:
    """Offer ``starts_at`` in place of a session the mentor just ended.

    Called after the transition, in its transaction. Raises
    :class:`ValidationError` at ``/suggested_starts_at`` when the caller is not
    the session's mentor, the session has no offering to book, or the time is
    not one the grid offers. Does not commit.
    """
    # **Scoped to the mentor in the query**: a mentee cancelling with a
    # suggestion selects nothing and is refused, and the transaction — the
    # cancellation included — goes with it.
    original = (
        (
            await session.execute(
                select(Session.mentor_id, Session.mentee_id, Session.session_type_id).where(
                    Session.id == session_id, Session.mentor_id == actor_id
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if original is None:
        raise _refuse("only the session's mentor may suggest another time")
    if original["session_type_id"] is None:
        raise _refuse("this session has no offering to book another time of")

    # **Locked before the grid is read**, so a booking racing this suggestion
    # either committed first (and the grid no longer offers the time) or waits
    # and then finds the hold (`holds.lock_mentor_slots`).
    await lock_mentor_slots(session, original["mentor_id"])
    day = starts_at.astimezone(dt.UTC).date()
    slots = await list_slots(
        session,
        original["mentor_id"],
        original["session_type_id"],
        start=day - dt.timedelta(days=SPAN_DAYS),
        end=day + dt.timedelta(days=SPAN_DAYS + 1),
        now=now,
        window=window,
        external_busy=external_busy,
    )
    slot = next((s for s in slots or () if s.start == starts_at), None)
    if slot is None:
        raise _refuse("that time is not available — re-read your slots")

    until = held_until(now)
    suggestion_id = (
        await session.execute(
            insert(SessionSuggestion)
            .values(
                session_id=session_id,
                mentor_id=original["mentor_id"],
                mentee_id=original["mentee_id"],
                session_type_id=original["session_type_id"],
                starts_at=starts_at,
                # The length the grid offered, which is what booking it will
                # snapshot too — so the hold covers exactly the hour it becomes.
                duration_minutes=int((slot.end - slot.start).total_seconds() // 60),
                held_until=until,
            )
            .returning(SessionSuggestion.id)
        )
    ).scalar_one()

    await enqueue(
        session,
        Notification.SESSION_TIME_SUGGESTED,
        entity_type="session",
        entity_id=session_id,
        recipient_ids=recipients(
            Notification.SESSION_TIME_SUGGESTED,
            mentor_id=original["mentor_id"],
            mentee_id=original["mentee_id"],
        ),
        variables={
            "reason_text": reason_text or "",
            "suggested_after": ended_as,
            "suggested_starts_at": starts_at.isoformat(),
            "held_until": until.isoformat(),
        },
    )

    # **Scheduled now, checked when it fires** (`remind_suggestion`). A failure
    # to schedule does not fail the suggestion: the offer stands and its hold
    # still lapses on time; the mentee is simply not nudged.
    at = suggestion_reminder_at(until)
    if scheduler is not None and callback_url and at > now:
        try:
            scheduler.schedule(
                url=callback_url,
                body={"session_id": str(session_id), "kind": SUGGESTION_REMINDER_KIND},
                at=at,
            )
        except (SchedulerError, NotImplementedError) as exc:
            logger.info("suggestion reminder for %s not scheduled: %s", session_id, exc)
    return UUID(str(suggestion_id))


async def remind_suggestion(session: AsyncSession, session_id: UUID, kind: str) -> bool:
    """Nudge the mentee, unless the offer was booked or has lapsed.

    **The re-read is the design**, as for every reminder: nothing is
    unscheduled, so a callback for an offer already booked finds nothing active
    and does nothing. Idempotent through the outbox's reminder index, so a
    QStash retry sends one email.
    """
    row = (
        (
            await session.execute(
                select(
                    SessionSuggestion.mentor_id,
                    SessionSuggestion.mentee_id,
                    SessionSuggestion.starts_at,
                    SessionSuggestion.held_until,
                ).where(
                    SessionSuggestion.session_id == session_id,
                    *active_hold(func.now()),
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return False
    await enqueue(
        session,
        Notification.SESSION_SUGGESTION_REMINDER,
        entity_type="session",
        entity_id=session_id,
        recipient_ids=recipients(
            Notification.SESSION_SUGGESTION_REMINDER,
            mentor_id=row["mentor_id"],
            mentee_id=row["mentee_id"],
        ),
        variables={
            "kind": kind,
            "suggested_starts_at": row["starts_at"].isoformat(),
            "held_until": row["held_until"].isoformat(),
        },
    )
    return True
