"""A booking's intake form as it stood at booking: ``intake_form_questions``.

Owner, 2026-10-10. Only answers were stored, never which questions were on the
form, so a mentor reading a booking could not tell "they skipped this" from "I
never asked this". The design draws every question with "No answer" under the
blanks. This table is the copy that makes that true: every question of the form,
answered or not, with the wording, type, order and requirement it had then.

**One row per question per submission**, the unique constraint saying so; it also
serves the per-booking read, since `submission_id` leads it. The submission
cascades, because the copy has no meaning without its booking. The question
restricts, as an answer's does, because a copy of a question is evidence of what
was asked, and questions are retired with `deleted_at` rather than removed.

**No backfill, and none is possible.** Nothing recorded the form before this, so
an older booking has no copy and its readers stay answers-only.

Expand only, and merged alone (project-conventions, *Migration ordering*): nothing
writes or reads the table until the next PR, so this lands inert. Creating a new
table locks only the tables it references, briefly, to add the foreign keys.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c4e2a9f1d63"
down_revision: str | Sequence[str] | None = "b3e9d1a7c452"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "intake_form_questions"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("question_type", sa.Text(), nullable=False),
        sa.Column("is_required", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
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
            "question_type IN ('free_text', 'file_upload', 'multi_choice')",
            name=op.f("ck_intake_form_questions_question_type_is_known"),
        ),
        sa.ForeignKeyConstraint(
            ["question_id"],
            ["session_type_questions.id"],
            name=op.f("fk_intake_form_questions_question_id_session_type_questions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["intake_submissions.id"],
            name=op.f("fk_intake_form_questions_submission_id_intake_submissions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_intake_form_questions")),
        sa.UniqueConstraint(
            "submission_id", "question_id", name=op.f("uq_intake_form_questions_submission_id")
        ),
    )
    op.execute(
        f"CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON {TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    """Drop the table.

    What it held is lost: which questions each booking's form asked. The answers
    themselves stay in `intake_answers`, so a booking reads answers-only again,
    exactly as one made before this table existed. Nothing else references it.
    """
    op.drop_table(TABLE)
