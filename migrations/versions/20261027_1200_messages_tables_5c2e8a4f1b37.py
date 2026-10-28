"""Messages: ``conversations``, ``conversation_participants``, ``messages``,
``message_attachments`` (ADR 0010; Messages PRD, founder 2026-10-26).

From the package's ``07_communications.sql``, reshaped by ADR 0010: one chat
per two people rather than per booking.

- ``conversations`` names its ``mentor_user_id`` and ``mentee_user_id`` and is
  unique on the **unordered** pair (``uq_conversations_pair``, on
  ``LEAST``/``GREATEST``), so two people have one chat whichever of them is the
  mentor. No ``last_message_preview`` (it depends on the reader) and no
  trigger: the send writes ``last_message_at`` in its own transaction. No
  ``deleted_at``: nothing deletes a chat.
- ``conversation_participants`` gets the generated key (ADR 0015) and
  ``UNIQUE (conversation_id, user_id)`` for the package's composite key. It is
  read state only; membership is the pair.
- ``messages`` gains ``client_id`` with ``UNIQUE (sender_id, client_id)``, so a
  retried send lands once. ``body`` is at most 2000 characters
  (``domain.chats.BODY_MAX_LENGTH``, pinned by a test) and a text message has
  one. ``ix_messages_conversation_sent`` serves the thread's keyset and the
  unread count.
- ``message_attachments.message_id`` is nullable and unique: a file is
  uploaded first and attached on send, one per message. Its type is the intake
  vocabulary; ``virus_scan_status`` is ``skipped`` (no scan in v1).

Vocabularies are text + CHECK (#100). User keys restrict, except a
participant's read state, which cascades (ADR 0013); every child of a chat
cascades from it.

Expand only, and merged alone (project-conventions, *Migration ordering*):
nothing reads or writes these tables until the next PR. Creating them takes a
brief lock on ``users`` to add the keys.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5c2e8a4f1b37"
down_revision: str | Sequence[str] | None = "4e8b2c6d1f93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("message_attachments", "messages", "conversation_participants", "conversations")
PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
#: Whether a table holds any row; the names come from `TABLES` above.
ANY_ROW = "SELECT EXISTS (SELECT 1 FROM {})"


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            name,
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        )
        for name in ("created_at", "updated_at")
    ]


def _id() -> sa.Column:
    return sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False)


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        "conversations",
        _id(),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("mentor_user_id", sa.Uuid(), nullable=False),
        sa.Column("mentee_user_id", sa.Uuid(), nullable=False),
        sa.Column("last_message_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("legacy_bubble_id", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted')",
            name=op.f("ck_conversations_status_is_known"),
        ),
        sa.CheckConstraint(
            "mentor_user_id <> mentee_user_id", name=op.f("ck_conversations_two_people")
        ),
        sa.ForeignKeyConstraint(
            ["mentor_user_id"],
            ["users.id"],
            name=op.f("fk_conversations_mentor_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["mentee_user_id"],
            ["users.id"],
            name=op.f("fk_conversations_mentee_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_conversations_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("legacy_bubble_id", name=op.f("uq_conversations_legacy_bubble_id")),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_conversations_pair ON conversations "
        "(LEAST(mentor_user_id, mentee_user_id), GREATEST(mentor_user_id, mentee_user_id))"
    )
    op.create_index("ix_conversations_mentor", "conversations", ["mentor_user_id"])
    op.create_index("ix_conversations_mentee", "conversations", ["mentee_user_id"])

    op.create_table(
        "conversation_participants",
        _id(),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "joined_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_read_at", sa.TIMESTAMP(timezone=True), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversation_participants")),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_conversation_participants_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_conversation_participants_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "conversation_id", "user_id", name="uq_conversation_participants_conversation_user"
        ),
    )
    op.create_index("ix_conversation_participants_user", "conversation_participants", ["user_id"])

    op.create_table(
        "messages",
        _id(),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("sender_id", sa.Uuid(), nullable=False),
        sa.Column("message_type", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column(
            "sent_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("edited_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("legacy_bubble_id", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.CheckConstraint(
            "message_type IN ('text', 'attachment')",
            name=op.f("ck_messages_message_type_is_known"),
        ),
        sa.CheckConstraint(
            "char_length(body) BETWEEN 1 AND 2000", name=op.f("ck_messages_body_length")
        ),
        sa.CheckConstraint(
            "message_type <> 'text' OR body IS NOT NULL",
            name=op.f("ck_messages_text_has_body"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_messages_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sender_id"],
            ["users.id"],
            name=op.f("fk_messages_sender_id_users"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("sender_id", "client_id", name="uq_messages_sender_client"),
        sa.UniqueConstraint("legacy_bubble_id", name=op.f("uq_messages_legacy_bubble_id")),
    )
    op.create_index(
        "ix_messages_conversation_sent", "messages", ["conversation_id", "sent_at", "id"]
    )

    op.create_table(
        "message_attachments",
        _id(),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("uploaded_by", sa.Uuid(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "virus_scan_status",
            sa.Text(),
            server_default=sa.text("'skipped'"),
            nullable=False,
        ),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_attachments")),
        sa.CheckConstraint(
            f"mime_type IN ('{PDF}', '{DOCX}')",
            name=op.f("ck_message_attachments_mime_type_is_known"),
        ),
        sa.CheckConstraint(
            "virus_scan_status IN ('skipped')",
            name=op.f("ck_message_attachments_virus_scan_status_is_known"),
        ),
        sa.CheckConstraint("size_bytes > 0", name=op.f("ck_message_attachments_size_is_positive")),
        sa.CheckConstraint(
            "char_length(file_name) BETWEEN 1 AND 255",
            name=op.f("ck_message_attachments_file_name_length"),
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_message_attachments_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by"],
            ["users.id"],
            name=op.f("fk_message_attachments_uploaded_by_users"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("message_id", name=op.f("uq_message_attachments_message_id")),
        sa.UniqueConstraint("storage_key", name=op.f("uq_message_attachments_storage_key")),
    )
    op.create_index(
        "ix_message_attachments_uploader", "message_attachments", ["uploaded_by", "created_at"]
    )
    op.create_index(
        "ix_message_attachments_unsent_created",
        "message_attachments",
        ["created_at"],
        postgresql_where=sa.text("message_id IS NULL"),
    )

    for table in TABLES:
        op.execute(
            f"CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )


def downgrade() -> None:
    """Drop the four tables. **Refuses while any holds a row**: a message is
    what somebody said to somebody, and a file row is the only record of an
    object in the private bucket, so neither is dropped by accident."""
    op.execute("SET lock_timeout = '3s'")
    bind = op.get_bind()
    for table in TABLES:
        if bind.execute(sa.text(ANY_ROW.format(table))).scalar():
            raise RuntimeError(f"{table} is not empty: copy its rows out before downgrading")
    for table in TABLES:
        op.drop_table(table)
