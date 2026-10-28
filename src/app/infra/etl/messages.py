"""Writing the legacy chats: conversations, their two participants, messages
and read marks, with Bubble's timestamps intact (decision 302).

**Idempotent, because the recovery plan is a re-run.** A chat upserts on the
pair (`uq_conversations_pair`), so two starters of one pair, a re-run, and a
chat the two already began on the new platform all land on one row; its
`legacy_bubble_id` is set once and kept. A message upserts on its own
`legacy_bubble_id`. A read mark only moves forward. The updated-at trigger is
held off (`timestamps_from_source_across`), as `ReviewLoader` explains.

**Who is the mentor is read here**, since the export does not say:
`transform.messages.chat_roles` decides from the newest booking between the
two, else the one mentor profile. Neither, or a person the users table does
not know, raises: the whole load rolls back with the chat named, rather than
guessing a role the M0 reads rely on.

**Returns nothing.** `reconcile_messages` reads back what landed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from app.domain.transform.messages import ChatRow, MessagePlan, chat_roles
from app.infra.db.triggers import timestamps_from_source_across

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = ["STAMPED_TABLES", "MessageLoader"]

#: The tables whose `updated_at` comes from the source.
STAMPED_TABLES = ("conversations", "messages")

NEWEST_BOOKED_MENTOR = """
SELECT mentor_id FROM sessions
WHERE (mentor_id = :a AND mentee_id = :b) OR (mentor_id = :b AND mentee_id = :a)
ORDER BY created_at DESC, id DESC
LIMIT 1
"""

PROFILE_HOLDERS = "SELECT user_id FROM mentor_profiles WHERE user_id IN (:a, :b)"

UPSERT_CHAT = """
INSERT INTO conversations (
    status, mentor_user_id, mentee_user_id, created_by, last_message_at,
    legacy_bubble_id, created_at, updated_at
)
VALUES (
    :status, :mentor, :mentee, :created_by, :last_message_at,
    :legacy_bubble_id, :created_at, :updated_at
)
ON CONFLICT ((LEAST(mentor_user_id, mentee_user_id)), (GREATEST(mentor_user_id, mentee_user_id)))
DO UPDATE SET
    legacy_bubble_id = COALESCE(conversations.legacy_bubble_id, EXCLUDED.legacy_bubble_id),
    status = CASE WHEN EXCLUDED.status = 'accepted' THEN 'accepted' ELSE conversations.status END,
    last_message_at = GREATEST(conversations.last_message_at, EXCLUDED.last_message_at),
    created_at = LEAST(conversations.created_at, EXCLUDED.created_at)
RETURNING id
"""

UPSERT_PARTICIPANT = """
INSERT INTO conversation_participants (conversation_id, user_id, joined_at, last_read_at)
VALUES (:conversation_id, :user_id, :joined_at, :last_read_at)
ON CONFLICT (conversation_id, user_id) DO UPDATE SET
    last_read_at = GREATEST(conversation_participants.last_read_at, EXCLUDED.last_read_at)
"""

UPSERT_MESSAGE = """
INSERT INTO messages (
    conversation_id, sender_id, message_type, body, client_id, sent_at,
    edited_at, deleted_at, legacy_bubble_id, created_at, updated_at
)
VALUES (
    :conversation_id, :sender_id, 'text', :body, NULL, :sent_at,
    :edited_at, :deleted_at, :legacy_bubble_id, :created_at, :updated_at
)
ON CONFLICT (legacy_bubble_id) DO UPDATE SET
    conversation_id = EXCLUDED.conversation_id,
    sender_id = EXCLUDED.sender_id,
    body = EXCLUDED.body,
    sent_at = EXCLUDED.sent_at,
    edited_at = EXCLUDED.edited_at,
    deleted_at = EXCLUDED.deleted_at,
    created_at = EXCLUDED.created_at,
    updated_at = EXCLUDED.updated_at
"""


class MessageLoader:
    """Three tables, inside the caller's transaction."""

    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def _roles(self, chat: ChatRow, a: UUID, b: UUID) -> tuple[UUID, UUID]:
        booked = (
            await self._connection.execute(text(NEWEST_BOOKED_MENTOR), {"a": a, "b": b})
        ).scalar()
        holders = set(
            (await self._connection.execute(text(PROFILE_HOLDERS), {"a": a, "b": b})).scalars()
        )
        roles = chat_roles((a, b), booked_mentor=booked, profile_holders=holders)
        if roles is None:
            message = (
                f"chat {chat.legacy_bubble_id}: neither a booking nor a mentor profile "
                "says which of the two is the mentor"
            )
            raise LookupError(message)
        return roles

    async def load(self, *, users: dict[str, UUID], plan: MessagePlan) -> None:
        """Write every planned chat, its participants, messages and read marks."""

        def user_id(bubble_id: str, what: str) -> UUID:
            resolved = users.get(bubble_id)
            if resolved is None:
                message = f"{what} references unknown user {bubble_id}"
                raise LookupError(message)
            return resolved

        if not plan.chats:
            return
        marks = {(mark.chat, mark.reader): mark.last_read_at for mark in plan.read_marks}
        conversation: dict[str, UUID] = {}
        async with timestamps_from_source_across(self._connection, STAMPED_TABLES):
            for chat in plan.chats:
                a, b = (user_id(p, f"chat {chat.legacy_bubble_id}") for p in chat.people)
                mentor, mentee = await self._roles(chat, a, b)
                chat_id = (
                    await self._connection.execute(
                        text(UPSERT_CHAT),
                        {
                            "status": chat.status.value,
                            "mentor": mentor,
                            "mentee": mentee,
                            "created_by": user_id(chat.started_by, "sendBy"),
                            "last_message_at": chat.last_message_at,
                            "legacy_bubble_id": chat.legacy_bubble_id,
                            "created_at": chat.created_at,
                            "updated_at": chat.updated_at,
                        },
                    )
                ).scalar_one()
                conversation[chat.legacy_bubble_id] = chat_id
                participants: list[dict[str, Any]] = [
                    {
                        "conversation_id": chat_id,
                        "user_id": user_id(person, "a participant"),
                        "joined_at": chat.created_at,
                        "last_read_at": marks.get((chat.legacy_bubble_id, person)),
                    }
                    for person in chat.people
                ]
                await self._connection.execute(text(UPSERT_PARTICIPANT), participants)
            if plan.messages:
                await self._connection.execute(
                    text(UPSERT_MESSAGE),
                    [
                        {
                            "conversation_id": conversation[row.chat],
                            "sender_id": user_id(row.sender, "sendBy"),
                            "body": row.body,
                            "sent_at": row.sent_at,
                            "edited_at": row.edited_at,
                            "deleted_at": row.deleted_at,
                            "legacy_bubble_id": row.legacy_bubble_id,
                            "created_at": row.created_at,
                            "updated_at": row.updated_at,
                        }
                        for row in plan.messages
                    ],
                )
