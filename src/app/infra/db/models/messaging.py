"""Chats between a mentor and a mentee (ADR 0010; Messages PRD).

    conversations              one chat per two people
    conversation_participants  each person's read state in a chat
    messages                   what was said, oldest first by `sent_at`
    message_attachments        a file, uploaded first and attached on send

From the package's `07_communications.sql`, reshaped by ADR 0010: the chat
belongs to the pair rather than to a booking, so `conversations` names its
mentor and mentee and is unique on the two. **Who may read or write a chat is
never stored here**: it is the M0 connection, read from `sessions` in the
statement, so a chat is unreachable without a booking between the two.
"""

import datetime
import uuid

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.chats import BODY_MAX_LENGTH
from app.domain.enums import AttachmentScanStatus, ConversationStatus, IntakeFileType, MessageType
from app.domain.intake_files import MAX_FILENAME_LENGTH
from app.infra.db.base import Base, TimestampMixin
from app.infra.db.types import check_is_known, str_enum


class Conversation(TimestampMixin, Base):
    """One chat between two people, whatever bookings they share.

    **Unique on the unordered pair** (`uq_conversations_pair`): two people have
    one chat, whichever of them is the mentor. The roles are the ones the pair
    had when the chat began. **Membership is these two columns**; the
    participant rows hold read state only, so they are never a second answer
    to who is in the chat.

    **No `last_message_preview`** (ADR 0010): the preview depends on who reads
    it ("You: …"), so it is built on read. `last_message_at` is written by the
    send, in the message's own transaction, never by a trigger. No `deleted_at`:
    nothing deletes a chat, and the legacy data has no soft delete to carry.
    """

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    status: Mapped[ConversationStatus] = mapped_column(str_enum(ConversationStatus), nullable=False)
    mentor_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    mentee_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    #: The newest message's `sent_at`, null until the first one.
    last_message_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    #: Who sent the first message; null only where the legacy data cannot say.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    legacy_bubble_id: Mapped[str | None] = mapped_column(Text, unique=True)

    __table_args__ = (
        CheckConstraint(check_is_known("status", ConversationStatus), name="status_is_known"),
        CheckConstraint("mentor_user_id <> mentee_user_id", name="two_people"),
        Index(
            "uq_conversations_pair",
            text("LEAST(mentor_user_id, mentee_user_id)"),
            text("GREATEST(mentor_user_id, mentee_user_id)"),
            unique=True,
        ),
        # "My chats": one side or the other is the caller.
        Index("ix_conversations_mentor", "mentor_user_id"),
        Index("ix_conversations_mentee", "mentee_user_id"),
    )


class ConversationParticipant(TimestampMixin, Base):
    """One person's read state in one chat.

    **Unread is the other person's messages after `last_read_at`**, not
    deleted (Messages PRD), and `last_read_at` only moves forward. A row is
    read state, not membership: `conversations` says who is in the chat.
    """

    __tablename__ = "conversation_participants"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    #: Cascades: read state means nothing without its person (ADR 0013).
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    joined_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    last_read_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "conversation_id", "user_id", name="uq_conversation_participants_conversation_user"
        ),
        Index("ix_conversation_participants_user", "user_id"),
    )


class Message(TimestampMixin, Base):
    """One message in a chat.

    **`client_id` makes a retried send land once**: unique per sender
    (`uq_messages_sender_client`), in the spirit of ADR 0024. Null on imported
    messages, which had none, and nulls never collide.

    **`sent_at` orders the chat**, with `id` breaking a tie, and the send
    stamps it with `clock_timestamp()` after locking the chat's row (never
    the `now()` default, which is the transaction's start), so within one chat the order of
    `sent_at` is the order messages became visible: a reader polling with
    `after` never misses one that committed late.

    **An imported message is exempt from the two body CHECKs**: it carries
    what Bubble held, which had no length limit and may be blank. Every
    message written here is bounded.

    The sender **restricts**: what someone said stays in the other person's
    chat when they close their account, which is what the closed-chat line
    promises ("You can still read your past messages").
    """

    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    sender_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    message_type: Mapped[MessageType] = mapped_column(str_enum(MessageType), nullable=False)
    #: Trimmed at the boundary. Null only on a message that is a file alone.
    body: Mapped[str | None] = mapped_column(Text)
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    sent_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    #: Only imported messages carry it in v1: editing is later.
    edited_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    #: Only imported messages carry it in v1: deleting is later.
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    legacy_bubble_id: Mapped[str | None] = mapped_column(Text, unique=True)
    #: When an email about this message was queued (decision 303): the unread
    #: email or the session-window one. Set once, in the transaction that
    #: queues it, so a message is never emailed twice.
    emailed_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    __table_args__ = (
        UniqueConstraint("sender_id", "client_id", name="uq_messages_sender_client"),
        CheckConstraint(check_is_known("message_type", MessageType), name="message_type_is_known"),
        CheckConstraint(
            f"legacy_bubble_id IS NOT NULL OR char_length(body) BETWEEN 1 AND {BODY_MAX_LENGTH}",
            name="body_length",
        ),
        CheckConstraint(
            f"legacy_bubble_id IS NOT NULL OR message_type <> '{MessageType.TEXT.value}' "
            "OR body IS NOT NULL",
            name="text_has_body",
        ),
        # The thread's keyset, and the unread count after a read mark.
        Index("ix_messages_conversation_sent", "conversation_id", "sent_at", "id"),
        # The unread email's hourly limit: when a chat last had one emailed.
        Index(
            "ix_messages_conversation_emailed",
            "conversation_id",
            "emailed_at",
            postgresql_where=text("emailed_at IS NOT NULL"),
        ),
    )


class MessageAttachment(TimestampMixin, Base):
    """A file in a message. **Uploaded first, attached on send**: `message_id`
    is null until the message that carries it is written, and an upload never
    sent is swept (Messages PRD; decision 302).

    One file per message (`uq_message_attachments_message_id`; nulls do not
    collide, so unsent uploads are free). `storage_key` is built from ids only,
    never the file's name, as an intake file's is. The type is the intake
    vocabulary, decided from the bytes, so both uploads read one list.
    """

    __tablename__ = "message_attachments"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("messages.id", ondelete="CASCADE")
    )
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    #: The name to offer on download, sanitised on the way in.
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[IntakeFileType] = mapped_column(str_enum(IntakeFileType), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    virus_scan_status: Mapped[AttachmentScanStatus] = mapped_column(
        str_enum(AttachmentScanStatus),
        nullable=False,
        server_default=text(f"'{AttachmentScanStatus.SKIPPED.value}'"),
    )

    __table_args__ = (
        UniqueConstraint("message_id"),
        UniqueConstraint("storage_key"),
        CheckConstraint(check_is_known("mime_type", IntakeFileType), name="mime_type_is_known"),
        CheckConstraint(
            check_is_known("virus_scan_status", AttachmentScanStatus),
            name="virus_scan_status_is_known",
        ),
        CheckConstraint("size_bytes > 0", name="size_is_positive"),
        CheckConstraint(
            f"char_length(file_name) BETWEEN 1 AND {MAX_FILENAME_LENGTH}",
            name="file_name_length",
        ),
        # The upload limit counts one person's recent uploads.
        Index("ix_message_attachments_uploader", "uploaded_by", "created_at"),
        # The sweep: unsent uploads, oldest first.
        Index(
            "ix_message_attachments_unsent_created",
            "created_at",
            postgresql_where=text("message_id IS NULL"),
        ),
    )
