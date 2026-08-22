"""Queueing a message with the fact that caused it, and draining the queue.

**The enqueue happens inside the caller's transaction, and that is the whole
point.** A session and the intent to tell somebody about it commit together or
neither does, so there is no window where a booking exists and nobody will ever
be told — and no window where somebody is told about a booking that rolled back.

**Sending inline was the alternative and it fails twice.** A booking would block
on a third party, so a slow provider makes a slow checkout; and a crash between
the commit and the send loses the message with nothing recording it was owed.

**The drain is a third sweep beside the other two.** It runs in
`scripts/settle_sessions.py`, which already exists, already has a schedule and
already fails loudly — three sweeps in one job is one place to notice a
failure, where three jobs is three.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.domain.messages import MessageContext
from app.domain.notifications import Channel, Notification
from app.infra.db.models.platform import OutboxEvent
from app.infra.db.models.sessions import Session
from app.infra.db.models.user import User

__all__ = ["MAX_ATTEMPTS", "drain", "enqueue"]

#: How many times a message is retried before it is left alone.
#:
#: **Bounded, because an unbounded retry is an outage amplifier**: a provider
#: refusing every request would otherwise be asked again by every row on every
#: sweep, forever. Five hourly attempts spans most of a working day, which is
#: long enough for a transient failure and short enough that a permanent one
#: stops being noise.
#:
#: A row at the limit stays `failed` with its error, so what was owed and never
#: delivered is answerable from the table rather than from a log.
MAX_ATTEMPTS = 5

#: How many to send per sweep. The queue is small and the limit is not about
#: load — it bounds how long one run holds a transaction open, which matters
#: because a slow provider makes every row slow at once.
BATCH = 100


async def enqueue(
    session: AsyncSession,
    notification: Notification,
    *,
    entity_type: str,
    entity_id: UUID,
    recipient_ids: tuple[UUID, ...],
    channel: Channel = Channel.EMAIL,
    variables: dict[str, Any] | None = None,
) -> None:
    """Record that these people are owed this message. Does not commit.

    One row per recipient rather than one carrying a list, so a send that fails
    for one person is retried for that person — where a single row would have to
    choose between resending to everybody and losing the rest.

    **The recipient is an id, not an address.** The drain resolves it at send
    time, so somebody who changes their email between the enqueue and the send
    is written to at the new one. A stored address would quietly get that wrong,
    and would put a second copy of a contact detail in a table nobody thinks of
    as holding one.
    """
    if not recipient_ids:
        return
    statement = insert(OutboxEvent).values(
        [
            {
                "event_type": notification,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "destination": channel,
                "payload": {"recipient_id": str(recipient_id), **(variables or {})},
            }
            for recipient_id in recipient_ids
        ]
    )
    # **A reminder queued twice is an identical email a mentor did not need**,
    # and QStash retries by design — so a second enqueue for the same session
    # and kind is a no-op rather than an error. Nothing else carries a `kind`,
    # so the index this defers to skips every other message and the conflict
    # target can never match one.
    await session.execute(statement.on_conflict_do_nothing(index_where=text("payload ? 'kind'")))


async def drain(
    session: AsyncSession,
    *,
    notifier: Any,
    now: dt.datetime,
    settings: Settings | None = None,
) -> dict[str, int]:
    """Send what is pending. Returns counts by outcome. Does not commit.

    **Each row is attempted once per sweep and its outcome recorded**, so a
    provider that is down costs one attempt per message per hour rather than a
    retry storm. `attempts` is incremented whatever happens, which is what makes
    `MAX_ATTEMPTS` a bound rather than a suggestion.

    **A recipient with no address is `skipped`, not `failed`.** The two are
    different questions: failed means we tried and the provider said no, skipped
    means there was never anywhere to send it. Conflating them would make the
    retry count meaningless and would hide a data problem inside a delivery one
    — which matters right now, because WhatsApp can reach nobody until the
    `phone_*` columns exist.
    """
    pending = (
        (
            await session.execute(
                select(
                    OutboxEvent.id,
                    OutboxEvent.event_type,
                    OutboxEvent.destination,
                    OutboxEvent.payload,
                    OutboxEvent.attempts,
                    # **Needed to load the context the variables come from.**
                    # A message is about something, and until now the drain did
                    # not have to know what.
                    OutboxEvent.entity_type,
                    OutboxEvent.entity_id,
                )
                .where(
                    OutboxEvent.status == "pending",
                    OutboxEvent.attempts < MAX_ATTEMPTS,
                )
                .order_by(OutboxEvent.created_at)
                .limit(BATCH)
                # **Skip-locked**, so two sweeps overlapping send each message
                # once between them rather than both sending all of them. The
                # schedule makes that unlikely and a retry after a slow run
                # makes it possible, and a duplicate here is a duplicate in
                # somebody's inbox.
                .with_for_update(skip_locked=True)
            )
        )
        .mappings()
        .all()
    )

    settings = settings or get_settings()

    counts = {"sent": 0, "failed": 0, "skipped": 0}
    for row in pending:
        recipient = UUID(str(row["payload"]["recipient_id"]))
        address = await _address_for(session, recipient, Channel(str(row["destination"])))
        if address is None:
            await _finish(session, row["id"], "skipped", row["attempts"], "no address")
            counts["skipped"] += 1
            continue
        try:
            notifier.send(
                notification=Notification(str(row["event_type"])),
                channel=Channel(str(row["destination"])),
                to=address,
                # **The context, not the values.** Which template this message
                # uses is a fact about the channel, so what it declares is too —
                # and a notifier with no templates must not need one looked up
                # on its behalf. The adapter builds what it asked for.
                context=await _context_for(session, row, recipient, settings),
                # **The row's own id is the idempotency key.** It is a UUID that
                # exists exactly once per message per recipient, so a retry
                # after a timeout replays the provider's answer rather than
                # sending twice — which is the failure this whole table would
                # otherwise be blamed for.
                idempotency_key=str(row["id"]),
            )
        except Exception as exc:
            await _finish(session, row["id"], "failed", row["attempts"], str(exc)[:500])
            counts["failed"] += 1
        else:
            await _finish(session, row["id"], "sent", row["attempts"], None, sent_at=now)
            counts["sent"] += 1
    return counts


async def _finish(
    session: AsyncSession,
    event_id: UUID,
    status: str,
    attempts: int,
    error: str | None,
    *,
    sent_at: dt.datetime | None = None,
) -> None:
    """Record the outcome and count the attempt.

    A `failed` row goes back to `pending` unless it has run out of attempts, so
    the next sweep picks it up — and stays `failed` when it has, which is what
    makes the table answerable for what was never delivered.
    """
    exhausted = status == "failed" and attempts + 1 >= MAX_ATTEMPTS
    await session.execute(
        update(OutboxEvent)
        .where(OutboxEvent.id == event_id)
        .values(
            status="pending" if status == "failed" and not exhausted else status,
            attempts=attempts + 1,
            error_detail=error,
            sent_at=sent_at,
        )
    )


async def _context_for(
    session: AsyncSession, row: Any, recipient: UUID, settings: Settings
) -> MessageContext:
    """Everything the resolvers may read, loaded by what the message is about.

    **Keyed on `entity_type`.** A session message loads a session and both
    parties; a message about a mentor's application or their calendar has no
    session at all, and asking for `sessionDate` on one of those is a template
    pointed at the wrong message — which `build_variables` refuses by name.
    """
    people = await _names_for(session, (recipient,))
    extras = {
        str(key): str(value) for key, value in dict(row["payload"]).items() if key != "recipient_id"
    }
    base = {
        "recipient_name": people.get(recipient, ("", "UTC"))[0],
        "recipient_timezone": people.get(recipient, ("", "UTC"))[1],
        "app_base_url": settings.app_base_url or "",
        "extras": extras,
    }

    if str(row["entity_type"]) != "session":
        return MessageContext(mentor_name="", mentee_name="", **base)  # type: ignore[arg-type]

    found = (
        (
            await session.execute(
                select(
                    Session.id,
                    Session.mentor_id,
                    Session.mentee_id,
                    Session.starts_at,
                    Session.topic,
                    Session.booking_message,
                    Session.meeting_provider,
                    Session.respond_by,
                ).where(Session.id == row["entity_id"])
            )
        )
        .mappings()
        .one_or_none()
    )
    if found is None:  # pragma: no cover - the row is written with the session
        return MessageContext(mentor_name="", mentee_name="", **base)  # type: ignore[arg-type]

    parties = await _names_for(session, (found["mentor_id"], found["mentee_id"], recipient))
    return MessageContext(
        recipient_name=parties.get(recipient, ("", "UTC"))[0],
        recipient_timezone=parties.get(recipient, ("", "UTC"))[1],
        mentor_name=parties.get(found["mentor_id"], ("", "UTC"))[0],
        mentee_name=parties.get(found["mentee_id"], ("", "UTC"))[0],
        starts_at=found["starts_at"],
        topic=found["topic"],
        detail=found["booking_message"],
        venue=VENUE_LABELS.get(str(found["meeting_provider"] or "")),
        respond_by=found["respond_by"],
        session_id=str(found["id"]),
        app_base_url=settings.app_base_url or "",
        extras=extras,
    )


#: What a mentee reads where the column says `google_meet`.
#:
#: **A label, never a URL.** `location` in a template is where the session
#: happens, and putting the meeting link there would hand the room out days
#: early — which the join window exists to prevent.
VENUE_LABELS = {
    "google_meet": "Google Meet",
    "daily": "Daily",
    "zoom": "Zoom",
    "custom": "A link your mentor will share",
}


async def _names_for(
    session: AsyncSession, user_ids: tuple[UUID, ...]
) -> dict[UUID, tuple[str, str]]:
    """Display name and timezone for each person, in one statement."""
    rows = (
        await session.execute(
            select(User.id, User.first_name, User.last_name, User.timezone).where(
                User.id.in_(set(user_ids))
            )
        )
    ).mappings()
    return {
        row["id"]: (
            " ".join(part for part in (row["first_name"], row["last_name"]) if part).strip(),
            str(row["timezone"] or "UTC"),
        )
        for row in rows
    }


async def _address_for(session: AsyncSession, user_id: UUID, channel: Channel) -> str | None:
    """Where to reach this person on this channel, or ``None``.

    **WhatsApp always returns ``None`` today**, and that is the data rather than
    a stub: the three `phone_*` columns are deferred, so nobody has a number.
    Written as a real branch rather than an exception so the drain's `skipped`
    path is exercised by the only channel that can currently produce it.
    """
    if channel is not Channel.EMAIL:
        return None
    return (
        await session.execute(
            select(User.email).where(User.id == user_id, User.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
