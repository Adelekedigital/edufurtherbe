"""What a new message's push says, read when its job fires (decision 312).

**Read at the moment of sending, never at the send.** Five seconds is long
enough for the recipient to read it, or for the sender to send another, and
either means this push should not go: the reader has seen it, and the later
message's own job will push the burst once.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.db.chat_store import other_party, unread, unread_total
from app.infra.db.message_emails import messages_to
from app.infra.db.models.messaging import Conversation, ConversationParticipant, Message
from app.infra.db.models.user import User

__all__ = ["MessagePushFacts", "message_push_facts"]


@dataclass(frozen=True)
class MessagePushFacts:
    recipient_id: UUID
    sender_id: UUID
    first_name: str | None
    last_name: str | None
    #: The recipient's unread messages now: the app icon's badge.
    unread: int


async def message_push_facts(session: AsyncSession, message_id: UUID) -> MessagePushFacts | None:
    """The push this message owes, or `None` when it owes none.

    None when the message is gone or deleted, the recipient can no longer
    read the chat, has read it, or a later unread message from the same sender
    exists (its job pushes instead). A closed account is left to
    `subscriptions_for`, which finds no browsers for one.
    """
    found = (
        await session.execute(
            select(
                Message.sender_id,
                other_party(Message.sender_id).label("recipient_id"),
            )
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(Message.id == message_id, Message.deleted_at.is_(None))
        )
    ).one_or_none()
    if found is None:
        return None
    sender, recipient = found.sender_id, found.recipient_id
    newest = await session.scalar(
        messages_to(recipient)
        .add_columns(Message.id)
        .where(
            Message.sender_id == sender,
            *unread(recipient, ConversationParticipant.last_read_at),
        )
        .order_by(Message.sent_at.desc(), Message.id.desc())
        .limit(1)
    )
    if newest != message_id:
        return None
    names = (
        await session.execute(select(User.first_name, User.last_name).where(User.id == sender))
    ).one()
    return MessagePushFacts(
        recipient_id=recipient,
        sender_id=sender,
        first_name=names.first_name,
        last_name=names.last_name,
        unread=await unread_total(session, recipient),
    )
