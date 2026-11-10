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
from collections.abc import Mapping, Sequence
from contextvars import ContextVar, Token
from typing import Any
from uuid import UUID

from sqlalchemy import Uuid, column, literal, select, text, true, update, values
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.errors import ConfigurationError
from app.domain.analytics_events import AnalyticsEvent, analytics_event
from app.domain.email_lines import OWNED_BY
from app.domain.message_emails import MESSAGE_EMAILS
from app.domain.messages import DELETED_PARTY_LABELS, MessageContext
from app.domain.names import addressed_by, joined_name
from app.domain.notifications import Channel, Notification
from app.domain.ports import AnalyticsSink
from app.domain.push import SESSION_PUSHES, session_push, session_push_spec
from app.infra.clients.web_push import PushOutcome, PushTarget, WebPushPort, fan_out
from app.infra.db.analytics_scope import counted_user
from app.infra.db.chat_store import unread_total
from app.infra.db.credit_store import expiring_on
from app.infra.db.email_lines import LineOverrides, load_line_overrides
from app.infra.db.holds import suggestion_reminder_state
from app.infra.db.mentor_listing import return_reminder_state
from app.infra.db.message_email_content import message_email_values
from app.infra.db.models.platform import OutboxEvent
from app.infra.db.models.push import PushSubscription
from app.infra.db.models.sessions import Session, SessionType
from app.infra.db.models.user import User
from app.infra.db.pending_requests import request_reminder_state
from app.infra.db.predicates import LIVE
from app.infra.db.push_store import forget_gone, subscriptions_for
from app.infra.db.session_answer_rows import answer_previews
from app.infra.db.session_store import is_a_party

__all__ = [
    "MAX_ATTEMPTS",
    "collect_queued",
    "deliver",
    "drain",
    "drain_analytics",
    "drain_push",
    "enqueue",
    "enqueue_analytics",
    "enqueue_analytics_each",
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

#: What each drain sweeps, **named rather than "everything else"**: a drain
#: that took every row but the push rows read an analytics event as a message,
#: found no recipient and aborted the sweep for everybody (PRD 30).
MESSAGE_CHANNELS = (Channel.EMAIL, Channel.WHATSAPP)
PUSH_CHANNELS = (Channel.PUSH,)
ANALYTICS_CHANNELS = (Channel.ANALYTICS,)

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
    rows = [
        {
            "event_type": notification,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "destination": channel,
            "payload": {"recipient_id": str(recipient_id), **(variables or {})},
        }
        for recipient_id in recipient_ids
    ]
    if channel is Channel.EMAIL and notification in SESSION_PUSHES:
        rows += await _push_rows(session, notification, entity_id, rows, recipient_ids)
    statement = insert(OutboxEvent).values(rows)
    # **A reminder queued twice is an identical email a mentor did not need**,
    # and QStash retries by design — so a second enqueue for the same session
    # and kind is a no-op rather than an error. **No conflict target is named**
    # (`index_where` alone is ignored), so any unique violation is a no-op; the
    # only unique index here is `uq_outbox_events_reminder`, partial on `kind`.
    inserted = await session.execute(
        statement.on_conflict_do_nothing(index_where=text("payload ? 'kind'")).returning(
            OutboxEvent.id
        )
    )
    collecting = _queued.get()
    if collecting is not None:
        collecting.extend(inserted.scalars().all())


async def enqueue_analytics(
    session: AsyncSession,
    event: AnalyticsEvent,
    user_id: UUID,
    properties: Mapping[str, object],
    *,
    at: dt.datetime,
) -> None:
    """Record that this event happened, for the analytics drain. Does not commit.
    :func:`enqueue_analytics_each` for one, so the skip below is written once."""
    await enqueue_analytics_each(session, event, [(user_id, properties)], at=at)


#: Events per statement: two parameters each, well inside asyncpg's 32,767.
ANALYTICS_CHUNK = 1000


async def enqueue_analytics_each(
    session: AsyncSession,
    event: AnalyticsEvent,
    facts: Sequence[tuple[UUID, Mapping[str, object]]],
    *,
    at: dt.datetime,
) -> None:
    """One `event` per ``(user_id, properties)``, all at ``at``. Does not commit.

    In the caller's transaction, like `enqueue`, so the event exists exactly
    when the fact it reports does. The payload is the event as the vendor's
    batch takes it (`analytics_event`), so the drain sends it as stored.

    **An internal account's event is never written** (PRD 30): the admin
    Analytics rule (`analytics_scope.counted_user`) filters the insert itself,
    so the two can never disagree about who is left out. A statement per chunk,
    not per event, so the 1st's grant to every mentee stays a few round trips.
    """
    for start in range(0, len(facts), ANALYTICS_CHUNK):
        chunk = facts[start : start + ANALYTICS_CHUNK]
        rows = values(column("user_id", Uuid), column("payload", JSONB), name="facts").data(
            [
                (user_id, analytics_event(event, user_id=user_id, at=at, properties=properties))
                for user_id, properties in chunk
            ]
        )
        await session.execute(
            insert(OutboxEvent).from_select(
                ["event_type", "entity_type", "entity_id", "destination", "payload"],
                select(
                    literal(event.value),
                    literal("user"),
                    rows.c.user_id,
                    literal(Channel.ANALYTICS.value),
                    rows.c.payload,
                ).where(counted_user(rows.c.user_id)),
            )
        )


async def _push_rows(
    session: AsyncSession,
    notification: Notification,
    session_id: UUID,
    rows: list[dict[str, Any]],
    recipient_ids: tuple[UUID, ...],
) -> list[dict[str, Any]]:
    """**A push row beside the email, for whoever it pushes and who has a
    browser** (decision 312): same event, one row per channel so each is sent
    and recorded on its own. Nobody subscribed, no row; nor for the party the
    push skips (`session_push_spec`), such as the mentee who just booked."""
    found = (
        await session.execute(
            select(Session.mentor_id, Session.rescheduled_from_session_id).where(
                Session.id == session_id
            )
        )
    ).one_or_none()
    if found is None:  # pragma: no cover - the row is written with the session
        return []
    browsers = set(
        await session.scalars(
            select(PushSubscription.user_id).where(PushSubscription.user_id.in_(recipient_ids))
        )
    )
    return [
        row | {"destination": Channel.PUSH}
        for row, recipient_id in zip(rows, recipient_ids, strict=True)
        if recipient_id in browsers
        and session_push_spec(
            notification,
            recipient_role="mentor" if recipient_id == found.mentor_id else "mentee",
            moved=found.rescheduled_from_session_id is not None,
        )
        is not None
    ]


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
    pending = await _pending(session, MESSAGE_CHANNELS, entity_id=entity_id, kind=kind, ids=ids)
    settings = settings or get_settings()
    # Admins' wording, once per sweep (decision 318), not once per message.
    overrides = await load_line_overrides(session) if pending else {}

    counts = {"sent": 0, "failed": 0, "skipped": 0}
    for row in pending:
        recipient = UUID(str(row["payload"]["recipient_id"]))
        check = STILL_DUE.get(Notification(str(row["event_type"])))
        state = await _state(session, row, now)
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
            context = await _context_for(session, row, recipient, settings, overrides)
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
        except ConfigurationError as exc:
            if check is None:
                await _finish(session, row["id"], "failed", row["attempts"], str(exc)[:500])
                counts["failed"] += 1
                continue
            # **No template mapped yet, on a message re-checked at send time**:
            # left pending with no attempt counted, so it goes out once the
            # operator maps one rather than failing for good meanwhile. Safe
            # only because `STILL_DUE` runs first on every sweep and drops it
            # once it stops being true; a message without that check would go
            # out late, so it keeps the bounded retry above.
            logger.warning("outbox row waits for configuration: %s", exc)
            continue
        except Exception as exc:
            await _finish(session, row["id"], "failed", row["attempts"], str(exc)[:500])
            counts["failed"] += 1
        else:
            await _finish(session, row["id"], "sent", row["attempts"], None, sent_at=now)
            counts["sent"] += 1
    return counts


async def drain_push(
    session: AsyncSession,
    *,
    web_push: WebPushPort,
    now: dt.datetime,
    entity_id: UUID | None = None,
    kind: str | None = None,
    ids: Sequence[UUID] | None = None,
) -> dict[str, int]:
    """Send the pending push rows (decision 312). Counts by outcome. Does not
    commit. Narrowed like `drain`; only `PUSH_CHANNELS` rows.

    **One attempt each**, never retried: a push is about now, and a retry an
    hour later would alert again the browsers that already had it. `STILL_DUE`
    applies as it does to the email. A row older than its push's `ttl` is
    skipped as `expired`, so a "starting soon" never arrives after the start.
    Sent if any browser took it; browsers whose service says gone are forgotten.
    """
    counts = {"sent": 0, "failed": 0, "skipped": 0}
    for row in await _pending(session, PUSH_CHANNELS, entity_id=entity_id, kind=kind, ids=ids):
        state = await _state(session, row, now)
        if state == "wait":
            continue
        status, why = (
            ("skipped", "superseded")
            if state == "stale"
            else await _push(session, row, web_push, now)
        )
        await _finish(
            session,
            row["id"],
            status,
            row["attempts"],
            why,
            sent_at=now if status == "sent" else None,
            final=True,
        )
        counts[status] += 1
    return counts


async def drain_analytics(
    session: AsyncSession,
    *,
    sink: AnalyticsSink,
    now: dt.datetime,
    max_batches: int = 10,
) -> dict[str, int]:
    """Send the pending analytics events, oldest first, `BATCH` to a request.
    Counts by outcome. Does not commit.

    **One request per batch, so a batch succeeds or fails whole**: every row
    in it is `sent`, or counts an attempt under `MAX_ATTEMPTS` and keeps the
    error, as a message does. A refused batch ends the run, so a vendor that
    is down is asked once an hour, not `max_batches` times. ``max_batches``
    bounds how long the run holds its row locks.
    """
    counts = {"sent": 0, "failed": 0}
    for _ in range(max_batches):
        rows = await _pending(session, ANALYTICS_CHANNELS, entity_id=None, kind=None, ids=None)
        if not rows:
            break
        try:
            # Each event's `uuid` is its row id, so a batch resent after a lost
            # answer is a duplicate PostHog drops (decision 137): "Events that
            # share the same uuid, event name, timestamp, and distinct_id are
            # treated as duplicates" (https://posthog.com/docs/data/events,
            # "Event deduplication"; /api/capture does not list the field).
            events = [{**row["payload"], "uuid": str(row["id"])} for row in rows]
            await asyncio.to_thread(sink.capture, events)
        except Exception as exc:
            for row in rows:
                await _finish(session, row["id"], "failed", row["attempts"], str(exc)[:500])
            counts["failed"] += len(rows)
            break
        for row in rows:
            await _finish(session, row["id"], "sent", row["attempts"], None, sent_at=now)
        counts["sent"] += len(rows)
        if len(rows) < BATCH:
            break
    return counts


async def _push(
    session: AsyncSession, row: Any, web_push: WebPushPort, now: dt.datetime
) -> tuple[str, str | None]:
    """Push one row's event to its recipient's browsers: the outcome and why."""
    recipient = UUID(str(row["payload"]["recipient_id"]))
    found = (
        await session.execute(
            select(Session.mentor_id, Session.mentee_id, Session.rescheduled_from_session_id).where(
                Session.id == row["entity_id"]
            )
        )
    ).one_or_none()
    if found is None:  # pragma: no cover - the row is written with the session
        return "skipped", "no session"
    role, other, other_role = (
        ("mentor", found.mentee_id, "mentee")
        if recipient == found.mentor_id
        else ("mentee", found.mentor_id, "mentor")
    )
    spec = session_push_spec(
        Notification(str(row["event_type"])),
        recipient_role=role,
        moved=found.rescheduled_from_session_id is not None,
    )
    if spec is None:  # pragma: no cover - `_push_rows` queues none
        return "skipped", "not pushed"
    # **What is left of its life**, checked before any other read and sent as
    # the push's `TTL`, so a late send also expires on time at the service.
    left = spec.ttl - (now - row["created_at"])
    if left <= dt.timedelta(0):
        return "skipped", "expired"
    browsers = await subscriptions_for(session, recipient)
    if not browsers:
        return "skipped", "no browser"
    named = (await _names_for(session, (other,))).get(other)
    push = session_push(
        spec,
        session_id=row["entity_id"],
        # A party who has since left is "your mentor", as in the email (#288).
        other_name=(
            addressed_by(named[2], named[0] or None)
            if named is not None
            else DELETED_PARTY_LABELS[other_role]
        ),
        unread=await unread_total(session, recipient),
        ttl=left,
    )
    targets = [PushTarget(b.endpoint, b.p256dh, b.auth) for b in browsers]
    outcomes = await asyncio.to_thread(fan_out, web_push, targets, push)
    await forget_gone(session, [b.id for b in browsers], outcomes)
    if PushOutcome.DELIVERED in outcomes:
        return "sent", None
    if all(outcome is PushOutcome.GONE for outcome in outcomes):
        return "skipped", "gone"
    return "failed", "no push service took it"


async def _pending(
    session: AsyncSession,
    destinations: tuple[Channel, ...],
    *,
    entity_id: UUID | None,
    kind: str | None,
    ids: Sequence[UUID] | None,
) -> Sequence[Any]:
    """The pending rows of one drain: those bound for its ``destinations``."""
    return (
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
                    # A push expires (`drain_push`).
                    OutboxEvent.created_at,
                )
                .where(
                    OutboxEvent.status == "pending",
                    OutboxEvent.attempts < MAX_ATTEMPTS,
                    OutboxEvent.destination.in_(destinations),
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


async def _state(session: AsyncSession, row: Any, now: dt.datetime) -> str:
    """`due`, or what `STILL_DUE` says of a message whose truth can lapse."""
    check = STILL_DUE.get(Notification(str(row["event_type"])))
    if check is None:
        return "due"
    return str(await check(session, row["entity_id"], dict(row["payload"]), now))


#: Messages whose truth can lapse while they wait to be sent, and how to ask.
#:
#: **Checked at send time**, because a row queued now can be retried an hour
#: later: a return reminder for a mentor who has since resumed, set a new date
#: or changed zone would be a false or early instruction. Every other message is
#: about something that already happened and stays true. `stale` is skipped,
#: not failed (nothing went wrong); `wait` stays pending. A member here also
#: waits, uncounted, for a template the operator has not mapped (`drain`).
STILL_DUE = {
    Notification.MENTOR_RETURN_REMINDER: return_reminder_state,
    # The offer may be booked, or its hold lapsed, before the drain reaches it —
    # and a suggestion queued before its template id was set must not go out
    # once there is nothing left to book.
    Notification.SESSION_TIME_SUGGESTED: suggestion_reminder_state,
    Notification.SESSION_SUGGESTION_REMINDER: suggestion_reminder_state,
    # A mentee's nudge (decision 294): not once the mentor has answered.
    Notification.MENTEE_REQUEST_REMINDER: request_reminder_state,
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
    final: bool = False,
) -> None:
    """Record the outcome and count the attempt.

    A `failed` row goes back to `pending` unless it has run out of attempts, so
    the next sweep picks it up — and stays `failed` when it has, which is what
    makes the table answerable for what was never delivered. A `final` row (a
    push) has one attempt.
    """
    exhausted = status == "failed" and (final or attempts + 1 >= MAX_ATTEMPTS)
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


#: Payload keys that name rows rather than carry words for the template.
CARRIED_IDS = frozenset({"recipient_id", "message_ids"})


async def _context_for(
    session: AsyncSession,
    row: Any,
    recipient: UUID,
    settings: Settings,
    overrides: LineOverrides,
) -> MessageContext:
    """Everything the resolvers may read, loaded by what the message is about.

    **Keyed on `entity_type`.** A session message loads a session and both
    parties; a message about a mentor's application or their calendar has no
    session at all, and asking for `sessionDate` on one of those is a template
    pointed at the wrong message — which `build_variables` refuses by name.
    """
    people = await _names_for(session, (recipient,))
    nobody = ("", "UTC", None)
    payload = dict(row["payload"])
    extras = {str(key): str(value) for key, value in payload.items() if key not in CARRIED_IDS}
    notification = Notification(str(row["event_type"]))
    if notification in MESSAGE_EMAILS:
        # Read from the messages at send time, and not sent once they are all
        # read, or any deleted, since it was queued (decision 303).
        values = await message_email_values(
            session, notification, recipient, payload.get("message_ids") or ()
        )
        if values is None:
            raise Superseded
        extras |= values
    base = {
        "recipient_name": people.get(recipient, nobody)[0],
        "recipient_timezone": people.get(recipient, nobody)[1],
        "recipient_first_name": people.get(recipient, nobody)[2],
        "app_base_url": settings.app_base_url or "",
        "extras": extras,
        "notification": notification,
        "line_overrides": overrides.get(str(notification), {}),
    }

    if str(row["entity_type"]) == "mentor_profile":
        # The profile is the mentor's: the return reminder goes to them, and an
        # application notice to the admins deciding it, both naming the mentor.
        # A deleted applicant reads as "your mentor", as a session party does.
        named = _party_name(
            await _names_for(session, (row["entity_id"],)), row["entity_id"], "mentor"
        )
        # The mentor is the recipient of their own reminder, not of the notice
        # the admins get.
        base["recipient_role"] = "mentor" if recipient == row["entity_id"] else ""
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
                    Session.duration_minutes,
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
    # Decision 334: the count the session page's preview shows, never the
    # answers, read only for the mentor, whose questions they are.
    if notification in OWNED_BY["answeredQuestions"] and recipient == found["mentor_id"]:
        previews = await answer_previews(session, [found["id"]], is_a_party(recipient))
        extras["answered_questions"] = str(previews.get(found["id"], {}).get("count", 0))
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
        duration_minutes=found["duration_minutes"],
        session_id=str(found["id"]),
        app_base_url=settings.app_base_url or "",
        extras=extras,
        notification=notification,
        line_overrides=overrides.get(str(notification), {}),
        recipient_role=(
            "mentor"
            if recipient == found["mentor_id"]
            else "mentee"
            if recipient == found["mentee_id"]
            else ""
        ),
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
            joined_name(row["first_name"], row["last_name"]),
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
    notifier: Any | None,
    web_push: WebPushPort | None,
    settings: Settings,
    ids: Sequence[UUID],
) -> None:
    """Send these rows now, in a session of their own, after the request that
    queued them has committed and responded: the emails through `notifier`,
    the pushes through `web_push`, each only when there is one.

    **Only these rows**: anything else pending is the sweep's. A row whose
    request rolled back never existed, so there is nothing to send. A failure is
    logged and left pending; the hourly sweep retries it, as it always has.
    """
    if not ids:
        return
    try:
        async with factory() as session:
            now = dt.datetime.now(dt.UTC)
            if notifier is not None:
                await drain(session, notifier=notifier, now=now, settings=settings, ids=ids)
            if web_push is not None:
                await drain_push(session, web_push=web_push, now=now, ids=ids)
            await session.commit()
    except Exception:
        logger.exception("could not send %d queued message(s) now; the sweep will retry", len(ids))
