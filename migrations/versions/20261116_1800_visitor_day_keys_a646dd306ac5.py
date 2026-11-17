"""Visitor day keys: one random key per UTC day (decision 348).

A profile link's ``visitor_key`` becomes an HMAC under the day's random key,
not under a long-lived secret. Each key is deleted once its day is over, so a
stored ``visitor_key`` cannot be linked to a person after that day.

- ``visitor_day_keys``: ``day`` (UTC date) and ``key`` (32 random bytes).
  ``UNIQUE (day)`` lets every process get-or-create the same key with
  ``ON CONFLICT (day) DO NOTHING``. Rule 10 keeps a ``uuid`` id. Insert and
  delete only, so ``created_at`` alone. Every constraint name is written out,
  so each one the database holds appears verbatim in source.

Expand only: nothing reads or writes the table until the code that does ships.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a646dd306ac5"
down_revision: str | Sequence[str] | None = "c569432b779f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        "visitor_day_keys",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("key", postgresql.BYTEA(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_visitor_day_keys")),
        sa.UniqueConstraint("day", name=op.f("uq_visitor_day_keys_day")),
        sa.CheckConstraint(
            "octet_length(key) = 32", name=op.f("ck_visitor_day_keys_key_is_32_bytes")
        ),
    )


def downgrade() -> None:
    """Drop the table; today's visits and shares then count afresh under a new key."""
    op.execute("SET lock_timeout = '3s'")
    op.drop_table("visitor_day_keys")
