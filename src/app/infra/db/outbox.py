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

import asyncio
import datetime as dt
import logging
from collections.abc import Sequence
from contextvars import ContextVar, Token
from typing import Any
from uuid import UUID

from sqlalchemy import select, text, true, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.domain.messages import DELETED_PARTY_LABELS, MessageContext
from app.domain.notifications import Channel, Notification
from app.infra.db.credit_store import expiring_on
from app.infra.db.holds import suggestion_reminder_state
from app.infra.db.mentor_listing import return_reminder_state
from app.infra.db.models.platform import OutboxEvent
from app.infra.db.models.sessions import Session, SessionType
from app.infra.db.models.user import User
from app.infra.db.predicates import LIVE

__all__ = [
    "MAX_ATTEMPTS",
    "collect_queued",
    "deliver",
    "drain",
    "enqueue",
    "stop_collecting",
]

logger = logging.getLogger(__name__)

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

#: The rows this request has queued, when a request is collecting them.
#:
#: **Set per request by the app** (`main.py`), so the response can send what the
#: request owes as soon as it has gone, rather than leaving it to the hourly
#: sweep (owner, 2026-10-09: urgent; a confirmation could arrive an hour late).
#: Unset in jobs and scripts, where `enqueue` behaves exactly as before.
_queued: ContextVar[list[UUID] | None] = ContextVar("outbox_queued", default=None)


def collect_queued() -> tuple[list[UUID], Token[list[UUID] | None]]:
    """Start collecting the rows queued in this context. Returns the list the
    ids will land in, and the token that stops collection."""
    rows: list[UUID] = []
    return rows, _queued.set(rows)


def stop_collecting(token: Token[list[UUID] | None]) -> None:
    _queued.reset(token)


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
    inserted = await session.execute(
        statement.on_conflict_do_nothing(index_where=text("payload ? 'kind'")).returning(
            OutboxEvent.id
        )
    )
    collecting = _queued.get()
    if collecting is not None:
        collecting.extend(inserted.scalars().all())


async def drain(
    session: AsyncSession,
    *,
    notifier: Any,
    now: dt.datetime,
    settings: Settings | None = None,
    entity_id: UUID | None = None,
    kind: str | None = None,
    ids: Sequence[UUID] | None = None,
) -> dict[str, int]:
    """Send what is pending. Returns counts by outcome. Does not commit.

    ``entity_id`` and ``kind`` narrow it to one message's rows (Codex on #397):
    the reminder callback sends the reminder it just queued, when it is due,
    rather than leaving it for the hourly sweep. Everything else is the sweep's.

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
                    OutboxEvent.entity_id == entity_id if entity_id is not None else true(),
                    OutboxEvent.payload["kind"].astext == kind if kind is not None else true(),
                    OutboxEvent.id.in_(ids) if ids is not None else true(),
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
        check = STILL_DUE.get(Notification(str(row["event_type"])))
        state = (
            "due"
            if check is None
            else await check(session, row["entity_id"], dict(row["payload"]), now)
        )
        if state == "stale":
            await _finish(session, row["id"], "skipped", row["attempts"], "superseded")
            counts["skipped"] += 1
            continue
        if state == "wait":
            # Still true, not yet due in the recipient's current zone: left
            # pending, attempts untouched, for a later run.
            continue
        address = await _address_for(session, recipient, Channel(str(row["destination"])))
        if address is None:
            await _finish(session, row["id"], "skipped", row["attempts"], "no address")
            counts["skipped"] += 1
            continue
        try:
            context = await _context_for(session, row, recipient, settings)
        except Superseded:
            await _finish(session, row["id"], "skipped", row["attempts"], "superseded")
            counts["skipped"] += 1
            continue
        except Exception as exc:
            await _finish(session, row["id"], "failed", row["attempts"], str(exc)[:500])
            counts["failed"] += 1
            continue
        try:
            # In a thread: a provider call blocks, and the reminder callback
            # runs this inside a request on the event loop.
            await asyncio.to_thread(
                notifier.send,
                notification=Notification(str(row["event_type"])),
                channel=Channel(str(row["destination"])),
                to=address,
                # **The context, not the values.** Which template this message
                # uses is a fact about the channel, so what it declares is too —
                # and a notifier with no templates must not need one looked up
                # on its behalf. The adapter builds what it asked for.
                context=context,
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


#: Messages whose truth can lapse while they wait to be sent, and how to ask.
#:
#: **Checked at send time**, because a row queued now can be retried an hour
#: later: a return reminder for a mentor who has since resumed, set a new date
#: or changed zone would be a false or early instruction. Every other message is
#: about something that already happened and stays true. `stale` is skipped,
#: not failed (nothing went wrong); `wait` stays pending.
STILL_DUE = {
    Notification.MENTOR_RETURN_REMINDER: return_reminder_state,
    # The offer may be booked, or its hold lapsed, before the drain reaches it —
    # and a suggestion queued before its template id was set must not go out
    # once there is nothing left to book.
    Notification.SESSION_TIME_SUGGESTED: suggestion_reminder_state,
    Notification.SESSION_SUGGESTION_REMINDER: suggestion_reminder_state,
}


class Superseded(Exception):  # noqa: N818 - a reason not to send, not an error
    """What the message would say stopped being true while it waited.

    Raised while building the context, from the same read that fills it, so
    there is no gap between deciding to send and what the email says.
    """


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
    nobody = ("", "UTC", None)
    extras = {
        str(key): str(value) for key, value in dict(row["payload"]).items() if key != "recipient_id"
    }
    base = {
        "recipient_name": people.get(recipient, nobody)[0],
        "recipient_timezone": people.get(recipient, nobody)[1],
        "recipient_first_name": people.get(recipient, nobody)[2],
        "app_base_url": settings.app_base_url or "",
        "extras": extras,
    }

    if str(row["entity_type"]) == "mentor_profile":
        # The profile is the mentor's: the return reminder goes to them, and an
        # application notice to the admins deciding it, both naming the mentor.
        # A deleted applicant reads as "your mentor", as a session party does.
        named = _party_name(
            await _names_for(session, (row["entity_id"],)), row["entity_id"], "mentor"
        )
        return MessageContext(mentor_name=named, mentee_name="", **base)  # type: ignore[arg-type]
    if str(row["entity_type"]) != "session":
        if row["event_type"] == Notification.CREDITS_EXPIRING and extras.get("expires_at"):
            left = await expiring_on(
                session,
                row["entity_id"],
                dt.datetime.fromisoformat(extras["expires_at"]),
                # The send's own moment, not the drain's start: a run that began
                # before the expiry must not count credits that lapsed mid-run.
                now=dt.datetime.now(dt.UTC),
            )
            if left == 0:
                # All spent since the sweep queued it: nothing is expiring.
                raise Superseded
            extras["credit_count"] = str(left)
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
                    # The offering's current name, retired or not, as #185 and
                    # the session read do: the subject when no topic was written.
                    SessionType.name.label("session_type_name"),
                )
                .outerjoin(SessionType, SessionType.id == Session.session_type_id)
                .where(Session.id == row["entity_id"])
            )
        )
        .mappings()
        .one_or_none()
    )
    if found is None:  # pragma: no cover - the row is written with the session
        return MessageContext(mentor_name="", mentee_name="", **base)  # type: ignore[arg-type]

    parties = await _names_for(session, (found["mentor_id"], found["mentee_id"], recipient))
    return MessageContext(
        recipient_name=parties.get(recipient, nobody)[0],
        recipient_timezone=parties.get(recipient, nobody)[1],
        recipient_first_name=parties.get(recipient, nobody)[2],
        mentor_name=_party_name(parties, found["mentor_id"], "mentor"),
        mentee_name=_party_name(parties, found["mentee_id"], "mentee"),
        starts_at=found["starts_at"],
        topic=found["topic"],
        session_type_name=found["session_type_name"],
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


def _party_name(people: dict[UUID, tuple[str, str, str | None]], user_id: UUID, role: str) -> str:
    """A session party's name, or "your mentor" / "your mentee" once they are gone.

    Absent from ``people`` means `_names_for` did not see them live: the account
    was deleted after the message was queued (#288). The message still goes to
    the recipient; it just names nobody who left.
    """
    found = people.get(user_id)
    return found[0] if found is not None else DELETED_PARTY_LABELS[role]


async def _names_for(
    session: AsyncSession, user_ids: tuple[UUID, ...]
) -> dict[UUID, tuple[str, str, str | None]]:
    """Display name, timezone and first name for each **live** person, in one statement.

    Through `LIVE`, like every other read of a person's identity (#212, #288):
    names are read when the outbox drains, which can be after an account was
    deleted, so a deleted person is simply absent here.
    """
    rows = (
        await session.execute(
            select(User.id, User.first_name, User.last_name, User.timezone).where(
                User.id.in_(set(user_ids)), LIVE
            )
        )
    ).mappings()
    return {
        row["id"]: (
            " ".join(part for part in (row["first_name"], row["last_name"]) if part).strip(),
            str(row["timezone"] or "UTC"),
            row["first_name"] or None,
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
        await session.execute(select(User.email).where(User.id == user_id, LIVE))
    ).scalar_one_or_none()


async def deliver(
    factory: async_sessionmaker[AsyncSession],
    *,
    notifier: Any,
    settings: Settings,
    ids: Sequence[UUID],
) -> None:
    """Send these rows now, in a session of their own, after the request that
    queued them has committed and responded.

    **Only these rows**: anything else pending is the sweep's. A row whose
    request rolled back never existed, so there is nothing to send. A failure is
    logged and left pending; the hourly sweep retries it, as it always has.
    """
    if not ids:
        return
    try:
        async with factory() as session:
            await drain(
                session, notifier=notifier, now=dt.datetime.now(dt.UTC), settings=settings, ids=ids
            )
            await session.commit()
    except Exception:
        logger.exception("could not send %d queued message(s) now; the sweep will retry", len(ids))
