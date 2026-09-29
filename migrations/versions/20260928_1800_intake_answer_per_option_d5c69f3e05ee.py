"""``intake_answers``: one row per chosen option for a multiple-choice answer.

Settled decision #207. Booking answers store a multiple-choice answer as one
row per selected option — the ``exactly_one_answer_form`` CHECK allows one
``selected_option_id`` per row, so several options mean several rows. The
old ``UNIQUE (submission_id, question_id)`` forbade that, so it becomes two
partial unique indexes that keep what it protected:

- ``ux_intake_answers_one_per_question``: text and file answers stay one per
  question (``selected_option_id IS NULL``);
- ``ux_intake_answers_one_per_option``: a choice answer names each option once.

Nothing writes ``intake_answers`` before this release, so there is no data to
move and both code versions serve during the deploy.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5c69f3e05ee"
down_revision: str | Sequence[str] | None = "ceb7410fb394"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "intake_answers"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.drop_constraint("uq_intake_answers_submission_id_question_id", TABLE, type_="unique")
    op.create_index(
        "ux_intake_answers_one_per_question",
        TABLE,
        ["submission_id", "question_id"],
        unique=True,
        postgresql_where=sa.text("selected_option_id IS NULL"),
    )
    op.create_index(
        "ux_intake_answers_one_per_option",
        TABLE,
        ["submission_id", "question_id", "selected_option_id"],
        unique=True,
        postgresql_where=sa.text("selected_option_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Restore one row per question. **Refuses while a multiple-choice answer
    holds several rows** — the constraint cannot be rebuilt over them, and
    deleting answers a mentee gave is not a downgrade's call to make."""
    op.execute("SET lock_timeout = '3s'")
    op.drop_index("ux_intake_answers_one_per_option", table_name=TABLE)
    op.drop_index("ux_intake_answers_one_per_question", table_name=TABLE)
    op.create_unique_constraint(
        "uq_intake_answers_submission_id_question_id", TABLE, ["submission_id", "question_id"]
    )
