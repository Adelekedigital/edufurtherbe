"""The design's six application stages join the vocabulary (expand step).

Session Types PRD, founder decisions 2026-10-21: the stages become *Exploring /
Researching schools & scholarships / Preparing documents / Applying / Preparing
for interview / Waiting for a decision*, with no custom stage. Slugs
``exploring``, ``researching``, ``preparing_documents``, ``applying``,
``interview_prep``, ``awaiting_decision``.

**Expand only.** Both ``CHECK``s — ``session_types.application_stage`` (the
legacy single field, #215) and ``session_type_stages.stage`` — widen to the old
six plus the new six, so the code running now keeps working and the next release
can write the new values. The contract migration remaps every stored row and
narrows both back to the new six; nothing is rewritten here.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1f4c8e2d6b9"
down_revision: str | Sequence[str] | None = "d4a9e2c7b5f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = (
    "early_exploration", "drafting_stage", "post_submission", "revisions", "interviewing", "other",
)  # fmt: skip
NEW = (
    "exploring", "researching", "preparing_documents", "applying", "interview_prep",
    "awaiting_decision",
)  # fmt: skip

LEGACY_CHECK = "ck_session_types_application_stage_is_known"
SET_CHECK = "ck_session_type_stages_stage_is_known"


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _checks(values: tuple[str, ...]) -> None:
    op.drop_constraint(op.f(LEGACY_CHECK), "session_types", type_="check")
    op.create_check_constraint(
        op.f(LEGACY_CHECK),
        "session_types",
        f"application_stage IS NULL OR {_in('application_stage', values)}",
    )
    op.drop_constraint(op.f(SET_CHECK), "session_type_stages", type_="check")
    op.create_check_constraint(op.f(SET_CHECK), "session_type_stages", _in("stage", values))


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    _checks(OLD + NEW)


def downgrade() -> None:
    """Narrow back to the old six.

    **Refuses while any row holds a new stage**, rather than guessing:
    ``researching`` and ``applying`` have no old equivalent, and dropping a
    mentor's choice silently is worse than a failed downgrade. Clear or remap
    them by hand first.
    """
    op.execute("SET lock_timeout = '3s'")
    # The six of `NEW`, written out: the statement carries no interpolation.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM session_types WHERE application_stage IN (
                    'exploring', 'researching', 'preparing_documents', 'applying',
                    'interview_prep', 'awaiting_decision'))
               OR EXISTS (
                SELECT 1 FROM session_type_stages WHERE stage IN (
                    'exploring', 'researching', 'preparing_documents', 'applying',
                    'interview_prep', 'awaiting_decision')) THEN
                RAISE EXCEPTION 'session types hold the new stages; remap them before downgrading';
            END IF;
        END $$
        """
    )
    _checks(OLD)
