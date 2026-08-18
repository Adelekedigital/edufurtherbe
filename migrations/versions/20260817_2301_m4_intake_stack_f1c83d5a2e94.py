"""The intake stack — four tables, landing together.

``session_type_questions``, ``session_type_question_options``,
``intake_submissions`` and ``intake_answers``. They were the four deliberately
deferred out of `04_sessions.sql` when M4 shipped, under settled decision #21:
none had a legacy source or a read surface, and the stack is a unit — a question
definition with nowhere to store an answer is a schema asserting a feature nobody
can use.

**This follows the canonical package rather than diverging from it**, so no ADR
accompanies it. Five things differ from `04_sessions.sql` and every one is a
standing rule this schema already applies to every table:

* ``question_type`` and ``intake_status`` are ``text`` + ``CHECK``, not
  PostgreSQL enum types (#100 — this schema has none left, and
  ``ENUM_TYPE_NAMES`` is asserted empty)
* index names take ``ix_``, where the package writes ``idx_``
* the ``updated_at`` trigger is attached **per table** rather than by the
  package's blanket ``attach_updated_at_triggers()`` scanner (#23: a scanner is
  non-deterministic across environments, invisible in a diff, and ``public`` is
  not exclusively ours on Supabase)
* a foreign key the package leaves without an action takes an explicit one,
  chosen by ADR 0013
* nothing to reconcile on keys: the package already gives all four a surrogate
  ``id``

**Deletion policy, and the one asymmetry worth reading twice.**

=================================  ==========  =================================
Foreign key                        Rule        Why
=================================  ==========  =================================
``questions.session_type_id``      CASCADE     meaningless without the offering
``options.question_id``            CASCADE     meaningless without the question
``submissions.session_id``         CASCADE     the form belongs to the booking
``answers.submission_id``          CASCADE     an answer belongs to its form
``answers.question_id``            RESTRICT    an answer is evidence of what was asked
``answers.selected_option_id``     RESTRICT    and of which choice was made
``questions.created_by``           RESTRICT    authorship is a record
``submissions.mentee_id``          RESTRICT    a submitted form is evidence
=================================  ==========  =================================

``answers.question_id`` restricting is what makes ``session_type_questions``
carry ``deleted_at``: a mentor editing their form would otherwise be refused by
every answer ever given to the question they are removing. Retiring the question
and keeping the answers is the shape that lets a form change without rewriting
history.

**``session_type_question_options`` ships with nothing writing it.** The UI's
intake screens use two of the three question types, and ``multi_choice`` is the
third the package declares. Dropping the table would be an undeclared divergence
from a document ADR 0007 makes authoritative — and #100's completion changed what
being early costs: the sub-rule against an unused vocabulary value existed
because ``ALTER TYPE ... ADD VALUE`` is permanent, and #107 recorded that it
"does not survive its removal". A ``CHECK`` value is now freely removed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1c83d5a2e94"
down_revision: str | Sequence[str] | None = "e5a71c9b382d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

QUESTION_TYPES = "'free_text', 'file_upload', 'multi_choice'"
INTAKE_STATUSES = "'draft', 'submitted', 'reviewed'"

#: Per table, per #23. The function already exists — M1 created it — so this
#: attaches rather than defines.
TRIGGER = (
    "CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON {table} "
    "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
)

TABLES = (
    "session_type_questions",
    "session_type_question_options",
    "intake_submissions",
    "intake_answers",
)


def upgrade() -> None:
    """Four tables in dependency order, then their triggers."""
    op.create_table(
        "session_type_questions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("session_type_id", sa.Uuid(), nullable=False),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("question_type", sa.Text(), nullable=False),
        sa.Column("is_required", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("display_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["session_type_id"],
            ["session_types.id"],
            name=op.f("fk_session_type_questions_session_type_id_session_types"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_session_type_questions_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_session_type_questions")),
        sa.CheckConstraint(
            f"question_type IN ({QUESTION_TYPES})",
            name=op.f("ck_session_type_questions_question_type_is_known"),
        ),
    )
    op.create_index(
        "ix_session_type_questions_form",
        "session_type_questions",
        ["session_type_id", "display_order"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "session_type_question_options",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.Column("option_text", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["question_id"],
            ["session_type_questions.id"],
            name=op.f("fk_session_type_question_options_question_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_session_type_question_options")),
    )
    op.create_index(
        "ix_session_type_question_options_question",
        "session_type_question_options",
        ["question_id", "sort_order"],
    )

    op.create_table(
        "intake_submissions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("mentee_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("submitted_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name=op.f("fk_intake_submissions_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["mentee_id"],
            ["users.id"],
            name=op.f("fk_intake_submissions_mentee_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_intake_submissions")),
        # ADR 0015: the surrogate key is `id`, and the 1:1 invariant the package
        # made the primary key is re-declared here.
        sa.UniqueConstraint("session_id", name=op.f("uq_intake_submissions_session_id")),
        sa.CheckConstraint(
            f"status IN ({INTAKE_STATUSES})",
            name=op.f("ck_intake_submissions_status_is_known"),
        ),
    )

    op.create_table(
        "intake_answers",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.Column("answer_text", sa.Text(), nullable=True),
        sa.Column("file_storage_key", sa.Text(), nullable=True),
        sa.Column("selected_option_id", sa.Uuid(), nullable=True),
        sa.Column(
            "answered_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
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
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["intake_submissions.id"],
            name=op.f("fk_intake_answers_submission_id_intake_submissions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["question_id"],
            ["session_type_questions.id"],
            name=op.f("fk_intake_answers_question_id_session_type_questions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["selected_option_id"],
            ["session_type_question_options.id"],
            name=op.f("fk_intake_answers_selected_option_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_intake_answers")),
        sa.UniqueConstraint(
            "submission_id", "question_id", name=op.f("uq_intake_answers_submission_id_question_id")
        ),
        # **Summed rather than chained.** `a IS NULL OR b IS NULL OR c IS NULL`
        # would permit an answer carrying nothing, which is a row that says a
        # question was answered and holds no answer. Counting refuses zero and
        # two alike.
        sa.CheckConstraint(
            "(answer_text IS NOT NULL)::int "
            "+ (file_storage_key IS NOT NULL)::int "
            "+ (selected_option_id IS NOT NULL)::int = 1",
            name=op.f("ck_intake_answers_exactly_one_answer_form"),
        ),
    )
    op.create_index("ix_intake_answers_submission", "intake_answers", ["submission_id"])

    for table in TABLES:
        op.execute(TRIGGER.format(table=table))


def downgrade() -> None:
    """Reverse dependency order. The triggers go with their tables."""
    op.drop_table("intake_answers")
    op.drop_table("intake_submissions")
    op.drop_table("session_type_question_options")
    op.drop_table("session_type_questions")
