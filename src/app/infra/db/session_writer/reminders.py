"""Reminders about a session: scheduling them when it is booked, firing
them when due, and chasing reviews that were never written.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    SessionStatus,
)
from app.domain.notifications import (
    REVIEW_REMINDER_AFTER,
    REVIEW_REMINDER_INTERVAL,
    REVIEW_REMINDER_KIND,
    SESSION_REMINDER_KINDS,
    Notification,
    recipients,
    session_reminders_for,
)
from app.infra.clients.scheduler import SchedulerError
from app.infra.db.models.platform import OutboxEvent
from app.infra.db.models.sessions import (
    Session,
)
from app.infra.db.outbox import enqueue
from app.infra.db.review_eligibility import already_reviewed

logger = logging.getLogger(__name__)


def schedule_session_reminders(
    session_id: UUID,
    starts_at: dt.datetime,
    *,
    scheduler: Any,
    callback_url: str,
    now: dt.datetime,
) -> int:
    """Publish the nudges for a session that is now real. Returns how many.

    **Called from both confirmation points, because there are two.** An
    auto-confirming offering is real at booking; one that waits is real at
    `/accept`. Scheduling only at booking would leave every
    confirmation-required session silently unreminded, and scheduling for a
    *pending* request would tell both parties their session is tomorrow when
    nobody has agreed to it yet.

    **A scheduling failure does not fail the thing that caused it**, for the
    reason booking already gives: the session exists and holds its slot, and the
    parties are simply not nudged. Refusing a confirmation because a scheduler
    was slow would lose something unrecoverable to protect something that is
    not.

    Not a coroutine: it touches no database, and its callers run it in a thread
    (#370), one hop for all four QStash calls. It is here rather than in `domain`
    because publishing is I/O, and here rather than in the route because both
    call sites would otherwise have to remember the same four arguments.
    """
    if scheduler is None or not callback_url:
        return 0
    published = 0
    for reminder, at in session_reminders_for(starts_at, now=now):
        try:
            scheduler.schedule(
                url=callback_url,
                body={"session_id": str(session_id), "kind": reminder.kind},
                at=at,
            )
        except (SchedulerError, NotImplementedError) as exc:
            logger.info(
                "session reminder %s for %s not scheduled: %s", reminder.kind, session_id, exc
            )
        else:
            published += 1
    return published


async def remind_before_session(session: AsyncSession, session_id: UUID, kind: str) -> bool:
    """Queue a pre-session nudge, unless the session is no longer happening.

    **The same re-read as the response reminder, for the same reason.** Nothing
    is ever cancelled: a callback for a session since cancelled, declined or
    withdrawn finds a status that is not `confirmed` and does nothing. Making
    four transitions responsible for unscheduling is how a reminder ends up
    arriving for a session that was called off through the one path somebody
    forgot.

    **Both parties**, unlike the response reminder — either can forget, and the
    message is about turning up rather than about answering.

    Idempotent through the partial unique index rather than through this read.
    QStash retries by design, so two callbacks arriving together would both see
    a confirmed session and the second insert is the one that no-ops.
    """
    reminder = SESSION_REMINDER_KINDS.get(kind)
    if reminder is None:  # pragma: no cover - the route checks first
        return False

    row = (
        (
            await session.execute(
                select(Session.status, Session.mentor_id, Session.mentee_id).where(
                    Session.id == session_id
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None or SessionStatus(row["status"]) is not SessionStatus.CONFIRMED:
        return False

    await enqueue(
        session,
        reminder.notification,
        entity_type="session",
        entity_id=session_id,
        recipient_ids=recipients(
            reminder.notification, mentor_id=row["mentor_id"], mentee_id=row["mentee_id"]
        ),
        # The words the template renders for `intervaltime`. They travel with
        # the schedule rather than being derived at send time, so changing the
        # offset moves the wording with it.
        variables={"kind": reminder.kind, "interval": reminder.interval},
    )
    return True


async def remind_unreviewed(session: AsyncSession, *, now: dt.datetime) -> int:
    """Ask again, a day on, wherever the review is still owed. Returns the count.

    **The same message, not a second one.** There is one template — a separate
    `REVIEW_REMINDER` member would need a second nobody wrote, and an unmapped
    member fails at the drain. The repeat carries `interval`, whose *absence*
    marks the first ask, so the template can say "we asked you a day ago" without
    the platform owning two vocabularies for one thing.

    **A sweep rather than a scheduled message, and that was measured.** The first
    version scheduled one QStash callback per settled session, following the
    shape `book_session` uses for pre-session reminders. That shape is built for
    *one* message on *one* request; this runs in a batch. Over 2,000 settled
    sessions it made 2,000 sequential HTTP calls, turning a 4-second sweep into a
    two-minute one, and the `Scheduler` port has no batch call to fix it with.

    A query costs one round trip whatever the volume, needs no QStash token in a
    batch job, and cannot leave a scheduled message behind for a review that has
    since been written — the condition *is* the query.

    **Anchored on the first ask specifically.** The outbox row for
    `REVIEW_REQUESTED` *without* a `kind` is the original; the repeat carries one.
    Reading every `REVIEW_REQUESTED` row would make this nudge its own nudge, a
    day at a time, for as long as the review went unwritten.

    It inherits the suppression for free: a session whose request the interval
    suppressed has no row here, so it is never asked again about something it was
    never asked about. Anchoring on `session_events` would have needed that rule
    restated.

    **The query asks whether a nudge is owed, not merely whether one is due**,
    and the difference is what the count means. Reading only "asked a day ago and
    still unreviewed" finds the same session on every subsequent run: the index
    makes the second insert a no-op, so one email goes out — but the sweep
    reports a nudge every night, forever, for a review nobody ever writes. An
    operator reading `nudged 1` would have no way to tell a fresh nudge from a
    permanent one.

    So the repeat's own row is part of the predicate. The partial unique index is
    then a **backstop** rather than the mechanism — settled decision #169's
    shape, where a pre-check races and the constraint answers too. It is still
    load-bearing: `uq_outbox_events_reminder` is partial on ``payload ? 'kind'``,
    so the first ask sits outside it and the repeat inside, which is what lets
    two rows of one message type coexist.

    Does not commit.
    """
    asked = OutboxEvent.__table__.alias("asked")
    nudged = OutboxEvent.__table__.alias("nudged")
    due = (
        (
            await session.execute(
                select(
                    asked.c.entity_id.label("session_id"),
                    Session.mentee_id,
                )
                .select_from(asked)
                .join(Session, Session.id == asked.c.entity_id)
                .where(
                    asked.c.event_type == Notification.REVIEW_REQUESTED,
                    ~asked.c.payload.has_key("kind"),
                    asked.c.created_at <= now - REVIEW_REMINDER_AFTER,
                    Session.status == SessionStatus.COMPLETED,
                    ~already_reviewed(Session.id, Session.mentee_id),
                    ~exists(
                        select(nudged.c.id).where(
                            nudged.c.entity_id == asked.c.entity_id,
                            nudged.c.event_type == Notification.REVIEW_REQUESTED,
                            nudged.c.payload["kind"].astext == REVIEW_REMINDER_KIND,
                        )
                    ),
                )
            )
        )
        .mappings()
        .all()
    )

    for row in due:
        await enqueue(
            session,
            Notification.REVIEW_REQUESTED,
            entity_type="session",
            entity_id=row["session_id"],
            recipient_ids=(row["mentee_id"],),
            variables={"kind": REVIEW_REMINDER_KIND, "interval": REVIEW_REMINDER_INTERVAL},
        )
    return len(due)


async def remind_if_still_waiting(session: AsyncSession, session_id: UUID, kind: str) -> bool:
    """Queue a reminder, unless the request has already been answered.

    **This re-read is the whole design.** Scheduling ahead normally obliges four
    transitions to unschedule, and the bug is the reminder that fires for a
    request answered through the one path somebody forgot. Checking at delivery
    makes that state unreachable rather than merely handled — nothing is ever
    cancelled, because a callback for a settled request simply does nothing.

    Returns whether anything was queued, so the route can say which happened
    without the caller inspecting the database.

    **Idempotent through the unique index**, not through this read: two
    callbacks arriving together would both see a pending request, and the second
    insert is the one that no-ops. QStash retries, so that is a real race rather
    than a theoretical one.
    """
    row = (
        (
            await session.execute(
                select(Session.status, Session.mentor_id, Session.mentee_id).where(
                    Session.id == session_id
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None or SessionStatus(row["status"]) is not SessionStatus.PENDING_MENTOR_APPROVAL:
        return False

    await enqueue(
        session,
        Notification.MENTOR_RESPONSE_REMINDER,
        entity_type="session",
        entity_id=session_id,
        recipient_ids=recipients(
            Notification.MENTOR_RESPONSE_REMINDER,
            mentor_id=row["mentor_id"],
            mentee_id=row["mentee_id"],
        ),
        variables={"kind": kind},
    )
    return True
