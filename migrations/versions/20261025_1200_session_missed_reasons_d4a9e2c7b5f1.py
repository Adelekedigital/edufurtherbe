"""``session_missed_reasons``: why a party missed a session, in their words.

Session Join PRD, founder decisions 2026-10-21. A party whose own attendance is
``no_show`` on a settled session may say why, once. Internal: admins read it,
the other party never does.

``id`` is the generated key (ADR 0015) and ``(session_id, user_id)`` is
``UNIQUE``: one reason per party per session, so a second send is refused. The
session key cascades, as ``session_participants``' does (a reason means nothing
without its session, and sessions are retained, so nothing reaches here through
a user deletion); the user key restricts, as the participant's does.
``reason`` is text + CHECK (#100); ``note`` is optional and at most 1000
characters (``domain.missed_reasons.NOTE_MAX_LENGTH``, pinned by a test).
Append-only, so ``created_at`` alone, no ``updated_at`` and no trigger.

Expand only, and merged alone (project-conventions, *Migration ordering*):
nothing reads or writes the table until the next PR, so this lands inert.
Creating a new table takes a brief lock on ``sessions`` and ``users`` to add the
keys.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4a9e2c7b5f1"
down_revision: str | Sequence[str] | None = "6f3f4bbaf321"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "session_missed_reasons"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "reason IN ('forgot', 'technical', 'emergency', 'wrong_time_zone', 'other')",
            name=op.f("ck_session_missed_reasons_reason_is_known"),
        ),
        sa.CheckConstraint(
            "char_length(note) <= 1000",
            name=op.f("ck_session_missed_reasons_note_length"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name=op.f("fk_session_missed_reasons_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_session_missed_reasons_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_session_missed_reasons")),
        sa.UniqueConstraint("session_id", "user_id", name="uq_session_missed_reasons_session_user"),
    )


def downgrade() -> None:
    """Drop the table.

    **What it holds is lost**: the reasons parties gave, with their notes. Run
    it only before anything writes the table, or after copying the rows out.
    Nothing else references the table.
    """
    op.execute("SET lock_timeout = '3s'")
    op.drop_table(TABLE)
