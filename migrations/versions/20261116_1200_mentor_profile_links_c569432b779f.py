"""Mentor profile link visits and shares (decision 346).

Tracked from launch, because a visit nobody recorded cannot be backfilled.

- ``mentor_profile_visits``: one row per visitor per mentor per UTC day.
  ``visitor_key`` is a 32-byte keyed hash of who was looking (never a raw
  address, browser or user id); ``UNIQUE (mentor_user_id, day, visitor_key)``
  makes recording ``ON CONFLICT DO NOTHING``, so a race cannot double-count.
  ``via`` (``copy``, ``share``, ``invite``) and ``referrer_host`` are the first
  visit's, both nullable.
- ``mentor_profile_shares``: one row per person per mentor per day per
  ``method`` (``copy``, ``native``), with ``own`` when the mentor shared their
  own profile, which only a signed-in sharer can be.

Rule 10 keeps a ``uuid`` id on each and re-declares the dedupe as ``UNIQUE``,
which also serves reading one mentor's days. Both cascade with the mentor.
Insert only, so ``created_at`` alone.

Expand only: nothing reads or writes either table until the code that does ships.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c569432b779f"
down_revision: str | Sequence[str] | None = "c4d81f2a6e57"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _common() -> list[sa.SchemaItem]:
    return [
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("mentor_user_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("visitor_key", postgresql.BYTEA(), nullable=False),
        sa.Column("signed_in", sa.Boolean(), nullable=False),
    ]


def _tail(table: str) -> list[sa.SchemaItem]:
    return [
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
        sa.ForeignKeyConstraint(
            ["mentor_user_id"],
            ["users.id"],
            name=op.f(f"fk_{table}_mentor_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "octet_length(visitor_key) = 32", name=op.f(f"ck_{table}_visitor_key_is_a_digest")
        ),
    ]


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        "mentor_profile_visits",
        *_common(),
        sa.Column("via", sa.Text(), nullable=True),
        sa.Column("referrer_host", sa.Text(), nullable=True),
        *_tail("mentor_profile_visits"),
        sa.UniqueConstraint(
            "mentor_user_id", "day", "visitor_key", name="uq_mentor_profile_visits_visitor_day"
        ),
        sa.CheckConstraint(
            "via IS NULL OR via IN ('copy', 'share', 'invite')",
            name=op.f("ck_mentor_profile_visits_via_is_known"),
        ),
        sa.CheckConstraint(
            "referrer_host IS NULL OR char_length(referrer_host) BETWEEN 1 AND 253",
            name=op.f("ck_mentor_profile_visits_referrer_host_fits"),
        ),
    )
    op.create_table(
        "mentor_profile_shares",
        *_common(),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("own", sa.Boolean(), nullable=False),
        *_tail("mentor_profile_shares"),
        sa.UniqueConstraint(
            "mentor_user_id",
            "day",
            "visitor_key",
            "method",
            name="uq_mentor_profile_shares_visitor_day_method",
        ),
        sa.CheckConstraint(
            "method IN ('copy', 'native')", name=op.f("ck_mentor_profile_shares_method_is_known")
        ),
        sa.CheckConstraint(
            "signed_in OR NOT own", name=op.f("ck_mentor_profile_shares_own_is_signed_in")
        ),
    )


def downgrade() -> None:
    """Drop both tables; the visits and shares recorded so far are lost."""
    op.execute("SET lock_timeout = '3s'")
    op.drop_table("mentor_profile_shares")
    op.drop_table("mentor_profile_visits")
