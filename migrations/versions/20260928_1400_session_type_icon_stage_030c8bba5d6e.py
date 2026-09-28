"""``session_types.icon``, and ``interviewing`` joins ``application_stage``.

Settled decision #197, Session Types frontend #18 and #10.

- ``icon``: one of the design's nine Material Symbols names, or NULL for the
  client's automatic pick. Text + CHECK (#100), values frozen here as the
  migration's own copy — the schema-parity test compares them with the enum.
- ``application_stage`` gains ``interviewing``, between ``revisions`` and
  ``other`` (the design's order). The CHECK is dropped and recreated with the
  wider list; every existing value is still in it, so validation passes.

Additive for a rolling deploy: old code never writes ``icon`` (NULL passes the
CHECK) and never writes ``interviewing``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "030c8bba5d6e"
down_revision: str | Sequence[str] | None = "24df9367576e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "session_types"
ICONS = (
    "video_call", "edit_document", "find_in_page", "school", "payments",
    "record_voice_over", "quiz", "badge", "lightbulb",
)  # fmt: skip
STAGES_BEFORE = ("early_exploration", "drafting_stage", "post_submission", "revisions", "other")
STAGES_AFTER = (
    "early_exploration", "drafting_stage", "post_submission", "revisions", "interviewing", "other",
)  # fmt: skip
STAGE_CHECK = "ck_session_types_application_stage_is_known"


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _stage_check(values: tuple[str, ...]) -> None:
    op.drop_constraint(op.f(STAGE_CHECK), TABLE, type_="check")
    op.create_check_constraint(
        op.f(STAGE_CHECK), TABLE, f"application_stage IS NULL OR {_in('application_stage', values)}"
    )


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.add_column(TABLE, sa.Column("icon", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_session_types_icon_is_known"), TABLE, f"icon IS NULL OR {_in('icon', ICONS)}"
    )
    _stage_check(STAGES_AFTER)


def downgrade() -> None:
    """Drop the icon; narrow the stage list back.

    **An offering aimed at ``interviewing`` loses its stage** — set to NULL, "any
    stage" — because the old vocabulary has no faithful equivalent and the
    narrower CHECK would refuse the row. Not reversible for those rows.
    """
    op.execute("SET lock_timeout = '3s'")
    op.execute(
        "UPDATE session_types SET application_stage = NULL WHERE application_stage = 'interviewing'"
    )
    _stage_check(STAGES_BEFORE)
    op.drop_constraint(op.f("ck_session_types_icon_is_known"), TABLE, type_="check")
    op.drop_column(TABLE, "icon")
