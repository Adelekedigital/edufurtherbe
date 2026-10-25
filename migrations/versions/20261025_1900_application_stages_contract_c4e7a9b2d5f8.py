"""The application stages are the design's six and nothing else (decision 288).

Step 3b, the contract. ``b8d2e6f4a3c7`` rewrote every stored row onto the six
and cleared every label, and since #536 no write can name anything else, so
both ``CHECK``s narrow to the six here:

- ``ck_session_types_application_stage_is_known`` and
  ``ck_session_type_stages_stage_is_known`` lose the retired six;
- ``ck_session_types_custom_stage_label_matches_stage`` (``other`` needs a
  label) goes with ``other``, and ``ck_session_types_no_custom_stage_label``
  says the column holds nothing, until the field leaves the API.

A row the remap missed would make this fail rather than pass silently, which is
the point of running it after the data step. The downgrade widens both back to
the previous revision's twelve and restores the label rule.

**`NOT VALID` is deliberately not used**, as in ``e5a71c9b382d`` on the same
table: a mentor holds at most five session types, so the validating scan is
short, and a constraint that exists without being validated looks enforced and
is not.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e7a9b2d5f8"
down_revision: str | Sequence[str] | None = "056f597d551a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SIX = (
    "exploring", "researching", "preparing_documents", "applying", "interview_prep",
    "awaiting_decision",
)  # fmt: skip
RETIRED = (
    "early_exploration", "drafting_stage", "post_submission", "revisions", "interviewing", "other",
)  # fmt: skip

LEGACY_CHECK = "ck_session_types_application_stage_is_known"
SET_CHECK = "ck_session_type_stages_stage_is_known"
LABEL_MATCH = "ck_session_types_custom_stage_label_matches_stage"
NO_LABEL = "ck_session_types_no_custom_stage_label"


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
    _checks(SIX)
    op.drop_constraint(op.f(LABEL_MATCH), "session_types", type_="check")
    op.create_check_constraint(op.f(NO_LABEL), "session_types", "custom_stage_label IS NULL")


def downgrade() -> None:
    """Back to the expand step's twelve, with the label rule restored. Nothing
    stored needs changing: every value here is legal there."""
    op.execute("SET lock_timeout = '3s'")
    op.drop_constraint(op.f(NO_LABEL), "session_types", type_="check")
    op.create_check_constraint(
        op.f(LABEL_MATCH),
        "session_types",
        "application_stage IS DISTINCT FROM 'other' OR custom_stage_label IS NOT NULL",
    )
    _checks(RETIRED + SIX)
