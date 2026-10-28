"""What a message email says, read when it is sent (decision 303).

The outbox row carries only the message ids; the drain reads them here, so the
email shows the names and files as they are at send time and the recipient's
own zone renders the times. **Read again on every attempt, from the same
statement that fills the email**: one with a deleted message, or whose
messages were all read since it was queued, is not sent (`None`), and neither
is an unread email to somebody who has switched them off since.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.domain.message_emails import (
    UNREAD_EMAILS,
    EmailedMessage,
    instant_email,
    unread_email,
)
from app.domain.notifications import Notification
from app.infra.db.chat_store import unread
from app.infra.db.message_prefs import unread_emails_on
from app.infra.db.models.messaging import (
    Conversation,
    ConversationParticipant,
    Message,
    MessageAttachment,
)
from app.infra.db.models.user import User
from app.infra.db.predicates import live

__all__ = ["message_email_values"]


async def message_email_values(
    session: AsyncSession, notification: Notification, recipient: UUID, ids: Sequence[Any]
) -> dict[str, str] | None:
    """The values for this email to ``recipient``, or ``None`` when it should
    no longer go. Only messages to ``recipient`` in their own chats are read."""
    sender = aliased(User, name="sender")
    mine = aliased(ConversationParticipant, name="mine")
    wanted = [UUID(str(value)) for value in ids]
    rows = (
        (
            await session.execute(
                select(
                    Message.sender_id,
                    Message.body,
                    Message.sent_at,
                    Message.deleted_at,
                    and_(*unread(recipient, mine.last_read_at)).label("unread"),
                    sender.first_name,
                    sender.last_name,
                    MessageAttachment.file_name,
                )
                .join(Conversation, Conversation.id == Message.conversation_id)
                .outerjoin(
                    mine,
                    and_(mine.conversation_id == Conversation.id, mine.user_id == recipient),
                )
                .outerjoin(sender, and_(sender.id == Message.sender_id, live(sender)))
                .outerjoin(MessageAttachment, MessageAttachment.message_id == Message.id)
                .where(
                    Message.id.in_(wanted),
                    Message.sender_id != recipient,
                    or_(
                        Conversation.mentor_user_id == recipient,
                        Conversation.mentee_user_id == recipient,
                    ),
                )
                .order_by(Message.sent_at, Message.id)
            )
        )
        .mappings()
        .all()
    )
    # Never a deleted message, never one gone with its chat, and never an
    # email about messages all read since it was queued.
    if not wanted or len(rows) != len(wanted):
        return None
    if any(row["deleted_at"] is not None for row in rows) or not any(row["unread"] for row in rows):
        return None
    if notification in UNREAD_EMAILS and not await unread_emails_on(session, recipient):
        return None
    messages = [
        EmailedMessage(
            sender_id=row["sender_id"],
            first_name=row["first_name"],
            last_name=row["last_name"],
            body=row["body"],
            file_name=row["file_name"],
            sent_at=row["sent_at"],
        )
        for row in rows
    ]
    if notification in UNREAD_EMAILS:
        return unread_email(messages)
    return instant_email(messages[0])
