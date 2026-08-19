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

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.notifications import Channel, Notification
from app.infra.db.models.platform import OutboxEvent
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
    await session.execute(
        insert(OutboxEvent),
        [
            {
                "event_type": notification,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "destination": channel,
                "payload": {"recipient_id": str(recipient_id), **(variables or {})},
            }
            for recipient_id in recipient_ids
        ],
    )


async def drain(session: AsyncSession, *, notifier: Any, now: dt.datetime) -> dict[str, int]:
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
                variables={
                    key: value
                    for key, value in dict(row["payload"]).items()
                    if key != "recipient_id"
                },
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
