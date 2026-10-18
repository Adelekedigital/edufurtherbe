"""The writing assistant's daily limit: ``writing_assist_requests``.

Mentee Onboarding v5 B7, owner 2026-10-17 (decision 266). Each user may ask the
writing assistant a bounded number of times a day per field. One row per call
actually made to the AI router; the limit counts a user's rows for one field
over the last 24 hours on the database clock, which
`ix_writing_assist_requests_user_field_created` serves.

**A table, where decision 235 counts existing rows**: writing assist saves
nothing, so no other row records that a call was made. **Append-only**, so
`created_at` alone and no `updated_at` or trigger. **No text**: what was sent
and what came back are never stored. `field` is text + CHECK (#100), one of
`mentee_bio` and `mentor_about`. The user key **cascades**: the rows only count
against a limit and are evidence of nothing.

Expand only, and merged alone (project-conventions, *Migration ordering*):
nothing reads or writes the table until the next PR, so this lands inert.
Creating a new table locks only `users`, briefly, to add the key.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a8d2e5c7f391"
down_revision: str | Sequence[str] | None = "f1a6c3e9b274"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "writing_assist_requests"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "field IN ('mentee_bio', 'mentor_about')",
            name=op.f("ck_writing_assist_requests_field_is_known"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_writing_assist_requests_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_writing_assist_requests")),
    )
    op.create_index(
        "ix_writing_assist_requests_user_field_created",
        TABLE,
        ["user_id", "field", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    """Drop the table.

    What it held is lost, which only resets every user's daily count: the rows
    record that calls were made, never what was written. Nothing else references
    the table.
    """
    op.execute("SET lock_timeout = '3s'")
    op.drop_table(TABLE)
