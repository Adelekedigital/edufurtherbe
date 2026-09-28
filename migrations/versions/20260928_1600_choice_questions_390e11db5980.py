"""``session_type_questions.allows_multiple``: single or multiple choice.

Settled decision #200, Session Types frontend #12. ``multi_choice`` is the one
choice type the canonical package declares (ADR 0007); this flag says whether
one or several options may be picked, so single choice needs no second value.
A CHECK keeps it false on every other type. The options themselves already have
their table (``session_type_question_options``).

Additive: a defaulted ``NOT NULL`` column (metadata-only on PostgreSQL 11+),
false on every existing row, which the CHECK allows — both code versions serve
during the deploy.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "390e11db5980"
down_revision: str | Sequence[str] | None = "030c8bba5d6e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "session_type_questions"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.add_column(
        TABLE,
        sa.Column("allows_multiple", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_session_type_questions_only_choice_allows_multiple"),
        TABLE,
        "NOT allows_multiple OR question_type = 'multi_choice'",
    )


def downgrade() -> None:
    """Drop the flag: every choice question reads as single choice again."""
    op.execute("SET lock_timeout = '3s'")
    op.drop_constraint(
        op.f("ck_session_type_questions_only_choice_allows_multiple"), TABLE, type_="check"
    )
    op.drop_column(TABLE, "allows_multiple")
