"""Sending a message and marking a chat read (ADR 0010; decision 301).

Both start from `chat_store.chat_of`, the caller's chat with this person
through M0, so a write into somebody else's chat, or into a chat with no
booking behind it, has no row to land on. Neither commits.

**A send is idempotent on `(sender_id, client_id)`.** A repeat finds the
stored message and returns it; two copies racing meet the unique key, and the
loser returns the winner's. **A send locks its chat's row before stamping
`sent_at`**, so within one chat `sent_at` is in the order messages became
visible and a reader polling with `after` never skips one that committed late.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    ConflictError,
    NotConnectedError,
    RecipientClosedError,
    RecipientPausedError,
    ValidationError,
)
from app.domain.chats import ChatLock, ChatRole, chat_lock
from app.domain.enums import ConversationStatus, MessageType
from app.infra.db.chat_store import (
    FILE_COLUMNS,
    MESSAGE_COLUMNS,
    chat_of,
    message_select,
    not_connected,
    pair,
)
from app.infra.db.message_file_store import attach_file
from app.infra.db.models.messaging import Conversation, ConversationParticipant, Message

__all__ = ["can_message", "lock_refusal", "mark_read", "send_message"]

#: A text-only message's file fields.
NO_FILE: dict[str, Any] = {column.key: None for column in FILE_COLUMNS}

#: `uq_conversations_pair`'s expressions, so a first send's `ON CONFLICT`
#: catches only the pair and any other unique violation still raises.
PAIR_INDEX = [
    text("LEAST(mentor_user_id, mentee_user_id)"),
    text("GREATEST(mentor_user_id, mentee_user_id)"),
]


async def _stored(session: AsyncSession, me: UUID, client_id: UUID) -> dict[str, Any] | None:
    """The caller's own message with this `client_id`, if it was sent."""
    row = (
        (
            await session.execute(
                message_select().where(Message.sender_id == me, Message.client_id == client_id)
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


def _replayed(stored: dict[str, Any], conversation_id: UUID | None) -> dict[str, Any]:
    """A stored message is the answer to its own retry, and only in its chat."""
    if stored["conversation_id"] != conversation_id:
        raise ValidationError(
            "client_id was already used for a message in another chat",
            field_errors=(("/client_id", "already used for a message in another chat"),),
        )
    return stored


def lock_refusal(chat: dict[str, Any]) -> ConflictError | None:
    """Why a send into this chat is refused by its lock (`chat_lock`), or
    ``None``. The send raises it; `can_message` asks it."""
    lock = chat_lock(closed=bool(chat["deleted"]), paused=bool(chat["paused"]))
    if lock is ChatLock.CLOSED:
        return RecipientClosedError("they closed their account")
    if lock is ChatLock.PAUSED:
        return RecipientPausedError("they are not taking messages right now")
    return None


def can_message(chat: dict[str, Any] | None) -> bool:
    """**The send's own rule, asked ahead** (the profile's `can_message`): the
    caller is connected (``chat`` is their `chat_of` row), the other person is
    their mentor in it, and nothing locks it."""
    return chat is not None and chat["role"] == ChatRole.MENTOR.value and lock_refusal(chat) is None


async def _open_chat(session: AsyncSession, me: UUID, other: UUID, their_role: str) -> UUID:
    """The pair's chat, created on the first send. Two first sends racing meet
    `uq_conversations_pair`: the second waits for the first, does nothing, and
    reads the chat the first made."""
    mentor, mentee = (other, me) if their_role == ChatRole.MENTOR.value else (me, other)
    created = (
        await session.execute(
            insert(Conversation)
            .values(
                status=ConversationStatus.ACCEPTED,
                mentor_user_id=mentor,
                mentee_user_id=mentee,
                created_by=me,
            )
            .on_conflict_do_nothing(index_elements=PAIR_INDEX)
            .returning(Conversation.id)
        )
    ).scalar_one_or_none()
    if created is None:
        created = (
            await session.execute(select(Conversation.id).where(pair(mentor, mentee)))
        ).scalar_one()
    await session.execute(
        insert(ConversationParticipant)
        .values([{"conversation_id": created, "user_id": user} for user in (mentor, mentee)])
        .on_conflict_do_nothing()
    )
    return created


async def send_message(
    session: AsyncSession,
    me: UUID,
    other: UUID,
    *,
    client_id: UUID,
    body: str | None,
    now: dt.datetime,
    file_id: UUID | None = None,
) -> tuple[dict[str, Any], bool]:
    """Send ``body``, ``file_id`` or both from ``me`` to ``other``: the message,
    and whether it is new (``False`` for a replay of ``client_id``). Does not
    commit.

    Raises, in this order: :class:`NotConnectedError` when the two are not
    connected (or ``other`` is nobody, or ``me``); a replay returns before the
    lock is read, so a retry after the other person paused still gets its
    message; :class:`RecipientClosedError` or :class:`RecipientPausedError`
    from `lock_refusal`; :class:`FileUnavailableError` from `attach_file`,
    which rolls the message back with the transaction.
    """
    chat = await chat_of(session, me, other, now)
    if chat is None:
        raise NotConnectedError("you can message only people you have booked with")
    stored = await _stored(session, me, client_id)
    if stored is not None:
        return _replayed(stored, chat["conversation_id"]), False
    if (refusal := lock_refusal(chat)) is not None:
        raise refusal

    conversation_id = chat["conversation_id"] or await _open_chat(
        session, me, other, str(chat["role"])
    )
    # The order of `sent_at` is the order of visibility: stamped under the
    # chat's lock, after any send ahead of this one has committed.
    await session.execute(
        select(Conversation.id)
        .where(Conversation.id == conversation_id)
        .with_for_update(key_share=True)
    )
    written = (
        (
            await session.execute(
                insert(Message)
                .values(
                    conversation_id=conversation_id,
                    sender_id=me,
                    client_id=client_id,
                    message_type=MessageType.TEXT if file_id is None else MessageType.ATTACHMENT,
                    body=body,
                    sent_at=func.clock_timestamp(),
                )
                .on_conflict_do_nothing(index_elements=["sender_id", "client_id"])
                .returning(*MESSAGE_COLUMNS)
            )
        )
        .mappings()
        .first()
    )
    if written is None:
        # The same send, racing: it committed first, so it is the answer.
        stored = await _stored(session, me, client_id)
        if stored is None:  # pragma: no cover - a conflict means the row exists
            raise RuntimeError("client_id conflicted with no stored message")
        return _replayed(stored, conversation_id), False
    file = (
        NO_FILE
        if file_id is None
        else await attach_file(session, file_id=file_id, sender_id=me, message_id=written["id"])
    )
    await session.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(last_message_at=written["sent_at"])
    )
    return {**written, **file}, True


async def mark_read(
    session: AsyncSession, me: UUID, other: UUID, up_to: UUID, now: dt.datetime
) -> None:
    """Move the caller's read mark in their chat with ``other`` to ``up_to``'s
    `sent_at`, **forward only**: an older mark is a no-op. Does not commit.

    :class:`NotFoundError` when the two are not connected; a 422 at `/up_to`
    when ``up_to`` is not a message in this chat.
    """
    chat = await chat_of(session, me, other, now)
    if chat is None:
        raise not_connected()
    sent_at = (
        await session.execute(
            select(Message.sent_at).where(
                Message.id == up_to, Message.conversation_id == chat["conversation_id"]
            )
        )
    ).scalar_one_or_none()
    if sent_at is None:
        raise ValidationError(
            "no such message in this chat",
            field_errors=(("/up_to", "not a message in this chat"),),
        )
    statement = insert(ConversationParticipant).values(
        conversation_id=chat["conversation_id"], user_id=me, last_read_at=sent_at
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["conversation_id", "user_id"],
            set_={"last_read_at": statement.excluded.last_read_at},
            where=or_(
                ConversationParticipant.last_read_at.is_(None),
                ConversationParticipant.last_read_at < statement.excluded.last_read_at,
            ),
        )
    )
