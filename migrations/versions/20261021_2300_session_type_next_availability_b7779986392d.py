"""``session_type_next_availability``: each live offering's next free time.

Mentor Profile frontend, 2026-10-21 (decision 284). The refresh job already
finds each offering's first slot and keeps the earliest for
``mentor_next_availability``; this keeps every offering's, so the profile can
show "next free" on each session-type card. A cache like its parent (ADR 0029):
nothing that decides anything reads it.

One row per (mentor, offering): ``id`` is the generated key (ADR 0015) and the
pair is ``UNIQUE``, as is ``session_type_id`` alone: an offering has one
mentor, so it has one row. Both foreign keys cascade, because a row is
meaningless without its mentor's profile or its offering. The ``CHECK`` holds the one
invariant the pair of times has: a deadline exactly when there is a time.

Expand only, and merged alone (project-conventions, *Migration ordering*):
nothing reads or writes the table until the next PR. No backfill: the first
refresh after that PR deploys writes the rows, and until then every offering
reads ``refreshing``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7779986392d"
down_revision: str | Sequence[str] | None = "c3e8a1f5d29b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "session_type_next_availability"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("mentor_user_id", sa.Uuid(), nullable=False),
        sa.Column("session_type_id", sa.Uuid(), nullable=False),
        sa.Column("next_available_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("bookable_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(next_available_at IS NULL) = (bookable_until IS NULL)",
            name=op.f("ck_session_type_next_availability_deadline_with_time"),
        ),
        sa.ForeignKeyConstraint(
            ["mentor_user_id"],
            ["mentor_profiles.user_id"],
            name="fk_session_type_next_availability_mentor",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_type_id"],
            ["session_types.id"],
            name="fk_session_type_next_availability_session_type",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_session_type_next_availability")),
    )
    op.create_index(
        "uq_session_type_next_availability_pair",
        TABLE,
        ["mentor_user_id", "session_type_id"],
        unique=True,
    )
    op.create_index(
        "uq_session_type_next_availability_session_type", TABLE, ["session_type_id"], unique=True
    )
    op.execute(
        f"CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON {TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    """Drop the table. A cache: the next refresh rebuilds every row."""
    op.execute("SET lock_timeout = '3s'")
    op.drop_table(TABLE)
