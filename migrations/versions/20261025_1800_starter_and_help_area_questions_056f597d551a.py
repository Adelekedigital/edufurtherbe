"""The starter offering, and choice questions whose options are the mentor's help areas.

Session Types PRD, founder decisions 2026-10-21 ("Intro chat: 'Created for you'
and the help-area question with synced options"). **The expand step**, all of it
additive; the code that writes and reads it ships next.

* ``session_types.is_starter``, ``NOT NULL DEFAULT false`` (metadata-only), and
  ``ix_session_types_one_starter``: at most one live starter per mentor, the
  shape of ``ix_session_types_one_featured`` (#217).
* **Backfill:** a live offering named exactly ``Intro chat`` is the starter.
  Mentor onboarding creates it through ``POST /me/session-types`` with that
  name, and nothing else in this repository writes it (the Bubble load writes
  ``General mentorship``, the demo seed its own names).
  ``ix_session_types_mentor_name`` already allows one live ``Intro chat`` per
  mentor, so the backfill cannot trip the new index. A mentor who renamed theirs
  is not marked; one who typed the name by hand is (dev data only so far).
* ``session_type_questions.options_source``, nullable text + ``CHECK`` (#100):
  ``help_areas`` or null, and only on ``multi_choice``.
* ``intake_answers.selected_offering_id``: the help area an answer chose,
  ``RESTRICT`` to ``service_offerings`` (never deleted, only retired).
  ``exactly_one_answer_form`` becomes a sum of four, ``ux_intake_answers_one_per_question``
  excludes these rows too, and ``ux_intake_answers_one_per_offering`` keeps each
  help area chosen once per answer.

**Rolling deploy:** old code never writes the new columns, and every row it
writes leaves ``selected_offering_id`` null, which satisfies the widened
``CHECK`` and the narrowed index predicate exactly as before.

**Downgrade** drops all of it. It **refuses while an answer holds a help area**:
the old ``CHECK`` cannot be rebuilt over such a row, and deleting what a mentee
answered is not a downgrade's call. Which offering was the starter is lost.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "056f597d551a"
down_revision: str | Sequence[str] | None = "b8d2e6f4a3c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STARTER_INDEX = "ix_session_types_one_starter"
SOURCE_KNOWN = "ck_session_type_questions_options_source_is_known"
SOURCE_CHOICE = "ck_session_type_questions_only_choice_has_options_source"
ANSWER_FORM = "ck_intake_answers_exactly_one_answer_form"
OFFERING_FK = "fk_intake_answers_selected_offering_id"
PER_QUESTION = "ux_intake_answers_one_per_question"
PER_OFFERING = "ux_intake_answers_one_per_offering"

THREE_FORMS = (
    "(answer_text IS NOT NULL)::int + (file_storage_key IS NOT NULL)::int "
    "+ (selected_option_id IS NOT NULL)::int = 1"
)
FOUR_FORMS = (
    "(answer_text IS NOT NULL)::int + (file_storage_key IS NOT NULL)::int "
    "+ (selected_option_id IS NOT NULL)::int + (selected_offering_id IS NOT NULL)::int = 1"
)


def _answer_form(body: str) -> None:
    op.drop_constraint(op.f(ANSWER_FORM), "intake_answers", type_="check")
    op.create_check_constraint(op.f(ANSWER_FORM), "intake_answers", body)


def _per_question(predicate: str) -> None:
    op.drop_index(PER_QUESTION, table_name="intake_answers")
    op.create_index(
        PER_QUESTION,
        "intake_answers",
        ["submission_id", "question_id"],
        unique=True,
        postgresql_where=sa.text(predicate),
    )


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    # The starter.
    op.add_column(
        "session_types",
        sa.Column("is_starter", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.execute(
        "UPDATE session_types SET is_starter = true "
        "WHERE name = 'Intro chat' AND deleted_at IS NULL"
    )
    op.create_index(
        STARTER_INDEX,
        "session_types",
        ["mentor_user_id"],
        unique=True,
        postgresql_where=sa.text("is_starter AND deleted_at IS NULL"),
    )

    # The question's options source.
    op.add_column("session_type_questions", sa.Column("options_source", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f(SOURCE_KNOWN),
        "session_type_questions",
        "options_source IS NULL OR options_source IN ('help_areas')",
    )
    op.create_check_constraint(
        op.f(SOURCE_CHOICE),
        "session_type_questions",
        "options_source IS NULL OR question_type = 'multi_choice'",
    )

    # The answer that chose a help area.
    op.add_column("intake_answers", sa.Column("selected_offering_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        OFFERING_FK,
        "intake_answers",
        "service_offerings",
        ["selected_offering_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    _answer_form(FOUR_FORMS)
    _per_question("selected_option_id IS NULL AND selected_offering_id IS NULL")
    op.create_index(
        PER_OFFERING,
        "intake_answers",
        ["submission_id", "question_id", "selected_offering_id"],
        unique=True,
        postgresql_where=sa.text("selected_offering_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM intake_answers WHERE selected_offering_id IS NOT NULL) THEN
                RAISE EXCEPTION 'intake answers hold help areas; remove them before downgrading';
            END IF;
        END $$;
        """
    )
    op.drop_index(PER_OFFERING, table_name="intake_answers")
    _per_question("selected_option_id IS NULL")
    _answer_form(THREE_FORMS)
    op.drop_constraint(OFFERING_FK, "intake_answers", type_="foreignkey")
    op.drop_column("intake_answers", "selected_offering_id")

    op.drop_constraint(op.f(SOURCE_CHOICE), "session_type_questions", type_="check")
    op.drop_constraint(op.f(SOURCE_KNOWN), "session_type_questions", type_="check")
    op.drop_column("session_type_questions", "options_source")

    op.drop_index(STARTER_INDEX, table_name="session_types")
    op.drop_column("session_types", "is_starter")
