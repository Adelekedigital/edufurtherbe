"""``user_activity_days``: the days each person used the platform (decision 342).

One row per person per UTC day they made a signed-in request (not in a
preview), for the admin Overview's weekly and monthly active users.

- ``(user_id, day)`` is ``UNIQUE``: recording is ``ON CONFLICT DO NOTHING``.
  Rule 10 keeps a ``uuid`` id and re-declares the pair here.
- ``(day, user_id)`` serves counting distinct people over a range of days.
- ``user_id`` cascades: a row means nothing without its person. The account
  purge anonymises rather than deletes, so it deletes these rows itself.
- Insert only, so ``created_at`` alone. No backfill: history starts at deploy
  (``users.last_active_at`` is migrated data and is not read).

Expand only: nothing reads or writes the table until the code that does ships.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e7a1c95b26"
down_revision: str | Sequence[str] | None = "f6b1d3a8c925"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        "user_activity_days",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_activity_days")),
        sa.UniqueConstraint("user_id", "day", name="uq_user_activity_days_user_day"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_activity_days_user_id_users"),
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_user_activity_days_day_user_id", "user_activity_days", ["day", "user_id"])


def downgrade() -> None:
    """Drop the table; the activity recorded so far is lost."""
    op.execute("SET lock_timeout = '3s'")
    op.drop_table("user_activity_days")
