"""Message emails: the switch, the unread email's job, and the session-window
email at send (Messages PRD, decision 303).

**One unread email per recipient at a time.** The job takes a per-recipient
advisory lock first, so two deliveries of one QStash job, or two jobs for the
same person, run one after the other: the second finds what the first stamped
and sends nothing. The stamp is also conditional (`emailed_at IS NULL`), so a
message is queued for at most one email even without the lock.

**`emailed_at` is set in the transaction that writes the outbox row**, so a
message is stamped exactly when an email about it is owed. What the email
says is loaded when it is sent (`message_email_content`), and an email whose
messages were all read, or any deleted, meanwhile is skipped there.

Unread is `chat_store.unread` over the recipient's chats through M0, the
badge's own rule. Nothing here commits.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.domain.message_emails import (
    UNREAD_EMAIL_INTERVAL,
    UNREAD_EMAIL_LOOKBACK,
    held_until,
    unread_kind,
)
from app.domain.notifications import Notification
from app.infra.db.chat_store import connections, in_session_window, pair, unread
from app.infra.db.message_prefs import unread_emails_on
from app.infra.db.models.messaging import Conversation, ConversationParticipant, Message
from app.infra.db.models.sessions import Session
from app.infra.db.models.user import User
from app.infra.db.outbox import enqueue
from app.infra.db.predicates import LIVE, live

__all__ = [
    "MESSAGE_EMAIL_LOCK_NAMESPACE",
    "UnreadEmailOutcome",
    "queue_instant_email",
    "queue_unread_email",
]

#: ``'MAIL'`` as four ASCII bytes: this module's namespace in the one global
#: keyspace `pg_advisory_xact_lock` shares with the credit, slot and image locks.
MESSAGE_EMAIL_LOCK_NAMESPACE = 0x4D41494C


@dataclass(frozen=True, slots=True)
class UnreadEmailOutcome:
    """What one run of the unread email job did."""

    #: The email queued, or ``None`` when nothing was.
    queued: Notification | None = None
    #: When to look again: an email went to this person within the hour.
    retry_at: dt.datetime | None = None
    #: Why nothing was queued, for the log.
    reason: str = "queued"


async def _lock(session: AsyncSession, recipient: UUID) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:ns, hashtext(:key))"),
        {"ns": MESSAGE_EMAIL_LOCK_NAMESPACE, "key": str(recipient)},
    )


def _to(recipient: UUID) -> Any:
    """Messages to ``recipient`` in their chats through M0, joined to their
    read mark as `mine`: the badge's own scope."""
    link = connections(recipient)
    return (
        select()
        .select_from(link)
        .join(Conversation, pair(recipient, link.c.other_id))
        .outerjoin(
            ConversationParticipant,
            and_(
                ConversationParticipant.conversation_id == Conversation.id,
                ConversationParticipant.user_id == recipient,
            ),
        )
        .join(Message, Message.conversation_id == Conversation.id)
        .where(Message.sender_id != recipient)
    )


async def _last_emailed(session: AsyncSession, recipient: UUID, now: dt.datetime) -> Any:
    """When a message to ``recipient`` was last emailed, within the hour."""
    return (
        await session.execute(
            _to(recipient)
            .add_columns(func.max(Message.emailed_at))
            .where(Message.emailed_at > now - UNREAD_EMAIL_INTERVAL)
        )
    ).scalar_one()


async def queue_unread_email(
    session: AsyncSession, recipient: UUID, now: dt.datetime
) -> UnreadEmailOutcome:
    """The unread email job for ``recipient``: queue the email their unread,
    unemailed messages make, or say why not. Does not commit.

    In order: the person is gone; they switched it off; nothing is unread,
    unemailed and inside `UNREAD_EMAIL_LOOKBACK` (read before the 15 minutes,
    or already emailed); an email went
    within the hour (``retry_at`` is when it ends). Only then are the messages
    stamped and the email queued, in this transaction.
    """
    await _lock(session, recipient)
    alive = (
        await session.execute(select(User.id).where(User.id == recipient, LIVE))
    ).scalar_one_or_none()
    if alive is None:
        return UnreadEmailOutcome(reason="recipient_gone")
    if not await unread_emails_on(session, recipient):
        return UnreadEmailOutcome(reason="switched_off")
    sender = aliased(User, name="sender")
    waiting = (
        (
            await session.execute(
                _to(recipient)
                .add_columns(Message.id)
                .join(sender, and_(sender.id == Message.sender_id, live(sender)))
                .where(
                    *unread(recipient, ConversationParticipant.last_read_at),
                    Message.emailed_at.is_(None),
                    Message.sent_at > now - UNREAD_EMAIL_LOOKBACK,
                )
                .order_by(Message.sent_at, Message.id)
            )
        )
        .scalars()
        .all()
    )
    if not waiting:
        return UnreadEmailOutcome(reason="nothing_unread")
    retry_at = held_until(await _last_emailed(session, recipient, now), now)
    if retry_at is not None:
        return UnreadEmailOutcome(retry_at=retry_at, reason="held")
    stamped = await _stamp(session, waiting, now)
    if not stamped:  # pragma: no cover - the lock makes the stamp ours
        return UnreadEmailOutcome(reason="nothing_unread")
    kind = unread_kind(len(stamped))
    await enqueue(
        session,
        kind,
        entity_type="user",
        entity_id=recipient,
        recipient_ids=(recipient,),
        variables={"message_ids": [str(message_id) for message_id in stamped]},
    )
    return UnreadEmailOutcome(queued=kind)


async def _stamp(session: AsyncSession, ids: Any, now: dt.datetime) -> list[UUID]:
    """Mark these messages emailed, those not already. Returns the ones marked,
    oldest first."""
    marked = set(
        (
            await session.execute(
                update(Message)
                .where(Message.id.in_(list(ids)), Message.emailed_at.is_(None))
                .values(emailed_at=now)
                .returning(Message.id)
            )
        )
        .scalars()
        .all()
    )
    return [message_id for message_id in ids if message_id in marked]


async def queue_instant_email(
    session: AsyncSession, message_id: UUID, sender: UUID, recipient: UUID, now: dt.datetime
) -> bool:
    """The session-window email for a message just sent, when a session between
    the two starts within `SESSION_WINDOW` of ``now``. Stamps the message so
    the unread email skips it. Returns whether it was queued. Does not commit.

    Always on and never held. The session is the one starting nearest ``now``.
    """
    found = (
        await session.execute(
            select(Session.id)
            .where(*in_session_window(sender, recipient, now))
            .order_by(func.abs(func.extract("epoch", Session.starts_at - now)), Session.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if found is None:
        return False
    if not await _stamp(session, [message_id], now):  # pragma: no cover - just written
        return False
    await enqueue(
        session,
        Notification.MESSAGE_IN_SESSION_WINDOW,
        entity_type="session",
        entity_id=found,
        recipient_ids=(recipient,),
        variables={"message_ids": [str(message_id)]},
    )
    return True
