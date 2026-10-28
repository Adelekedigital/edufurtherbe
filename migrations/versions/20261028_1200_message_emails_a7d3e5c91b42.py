"""Message emails: ``messages.emailed_at`` and ``notification_channel_preferences``
(Messages PRD, decision 303).

- ``messages.emailed_at`` (nullable): when an email about the message was
  queued, the unread email or the session-window one, so none is emailed twice.
  ``ix_messages_conversation_emailed`` (partial, ``emailed_at IS NOT NULL``)
  serves the unread email's hourly limit.
- ``notification_channel_preferences``, from the package's
  ``07_communications.sql``: the composite key becomes the generated ``id``
  (ADR 0015) plus ``UNIQUE (user_id, notification_type)``.
  ``notification_type`` is text + CHECK (#100), today only
  ``unread_messages``. A missing row means the defaults; the person cascades.

Expand only, and merged alone (project-conventions, *Migration ordering*):
nothing reads or writes either until the next PR. Adding a nullable column is a
catalogue change; the index is built on a table nothing writes yet.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7d3e5c91b42"
down_revision: str | Sequence[str] | None = "5c2e8a4f1b37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "notification_channel_preferences"
#: Whether the table holds any row.
ANY_ROW = "SELECT EXISTS (SELECT 1 FROM {})"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.add_column("messages", sa.Column("emailed_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.create_index(
        "ix_messages_conversation_emailed",
        "messages",
        ["conversation_id", "emailed_at"],
        postgresql_where=sa.text("emailed_at IS NOT NULL"),
    )

    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("notification_type", sa.Text(), nullable=False),
        sa.Column("in_app", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("email", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("push", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("whatsapp", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        *(
            sa.Column(
                name,
                sa.TIMESTAMP(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            )
            for name in ("created_at", "updated_at")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_channel_preferences")),
        sa.CheckConstraint(
            "notification_type IN ('unread_messages')",
            name=op.f("ck_notification_channel_preferences_notification_type_is_known"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notification_channel_preferences_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "user_id", "notification_type", name="uq_notification_channel_preferences_user_type"
        ),
    )
    op.execute(
        f"CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON {TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    """Drop both. **Refuses while a preference exists or a message was
    emailed**: losing a switched-off preference would start emails somebody
    turned off, and losing `emailed_at` would email messages a second time."""
    op.execute("SET lock_timeout = '3s'")
    bind = op.get_bind()
    if bind.execute(sa.text(ANY_ROW.format(TABLE))).scalar():
        raise RuntimeError(f"{TABLE} is not empty: copy its rows out before downgrading")
    if bind.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM messages WHERE emailed_at IS NOT NULL)")
    ).scalar():
        raise RuntimeError("messages.emailed_at is set: downgrading would email them again")
    op.drop_table(TABLE)
    op.drop_index("ix_messages_conversation_emailed", table_name="messages")
    op.drop_column("messages", "emailed_at")
