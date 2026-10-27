"""``mentor_admin_notes``: internal notes admins leave on a mentor.

Admin PRD, 2026-10-26: the review drawer carries notes only admins see. One row
per note, append-only, so ``created_at`` alone, no ``updated_at`` and no trigger.

``id`` is the generated key (ADR 0015). The mentor key cascades from
``mentor_profiles``, as ``mentor_guide_countries``' does (a hard delete of a
mentor would take their notes, intended; nothing makes one); the author key
restricts, as ``admin_users``' keys do. ``body`` is 1 to 2000 characters
(``domain.admin_notes.NOTE_BODY_MAX_LENGTH``, pinned by a test).
``ix_mentor_admin_notes_mentor`` serves the drawer's read, one mentor's notes
oldest first.

Expand only, and merged alone (project-conventions, *Migration ordering*):
nothing reads or writes the table until the next PR, so this lands inert.
Creating it takes a brief lock on ``mentor_profiles`` and ``users`` to add the
keys.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9d3b7f1a2c64"
down_revision: str | Sequence[str] | None = "8489be263031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "mentor_admin_notes"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("mentor_user_id", sa.Uuid(), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(body) BETWEEN 1 AND 2000",
            name=op.f("ck_mentor_admin_notes_body_length"),
        ),
        sa.ForeignKeyConstraint(
            ["mentor_user_id"],
            ["mentor_profiles.user_id"],
            name=op.f("fk_mentor_admin_notes_mentor_user_id_mentor_profiles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["users.id"],
            name=op.f("fk_mentor_admin_notes_author_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mentor_admin_notes")),
    )
    op.create_index(
        "ix_mentor_admin_notes_mentor", TABLE, ["mentor_user_id", "created_at"], unique=False
    )


def downgrade() -> None:
    """Drop the table.

    **What it holds is lost**: every note admins wrote. Run it only before
    anything writes the table, or after copying the rows out. Nothing else
    references the table.
    """
    op.execute("SET lock_timeout = '3s'")
    op.drop_index("ix_mentor_admin_notes_mentor", table_name=TABLE)
    op.drop_table(TABLE)
