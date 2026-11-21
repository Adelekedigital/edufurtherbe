"""User dismissed notices: the one-time notices a user has closed (decision 356).

- ``user_dismissed_notices``: ``user_id``, ``notice_key`` (the frontend's name
  for the notice, e.g. ``whats-new:2026-11-migration``) and ``created_at``,
  the first time it was dismissed. ``UNIQUE (user_id, notice_key)`` makes a
  repeat a no-op and serves every lookup by user. Rule 10 keeps a ``uuid`` id.
  Insert and delete only, so no ``updated_at``. Deleting a user deletes their
  rows. Every constraint name is written out, so each one the database holds
  appears verbatim in source.

The key's shape is ``domain.seen_items.KEY_PATTERN`` at the time of writing,
copied as a literal because a migration must not change when the domain does.

Expand only: nothing reads or writes the table until the code that does ships.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "db6fbb2aae43"
down_revision: str | Sequence[str] | None = "a646dd306ac5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KEY_PATTERN = "^[a-z0-9][a-z0-9:._-]{0,79}$"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        "user_dismissed_notices",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("notice_key", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_dismissed_notices")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_dismissed_notices_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "user_id", "notice_key", name="uq_user_dismissed_notices_user_id_notice_key"
        ),
        sa.CheckConstraint(
            f"notice_key ~ '{KEY_PATTERN}'",
            name=op.f("ck_user_dismissed_notices_notice_key_shape"),
        ),
    )


def downgrade() -> None:
    """Drop the table; every notice then shows once more to everyone."""
    op.execute("SET lock_timeout = '3s'")
    op.drop_table("user_dismissed_notices")
