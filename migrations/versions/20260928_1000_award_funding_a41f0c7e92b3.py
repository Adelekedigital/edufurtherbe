"""``user_awards.funding``: how an award was funded, as the holder says (#189).

``full``, ``partial``, or NULL for "not said". Self-reported, like every other
field on an award. Nothing in the migrated data carries it — no award field, and
``scholarship_programs.funding_type`` is empty on every row — so every existing
row reads NULL, which is exactly "not said".

Text + CHECK (#100); the ``IN`` permits NULL without a special case.

Additive: a nullable column with no default is metadata-only, and the CHECK
holds for every existing row (all NULL), so both code versions serve during the
deploy. Old code never reads or writes the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a41f0c7e92b3"
down_revision: str | Sequence[str] | None = "58725a5c5ed8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "user_awards"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.add_column(TABLE, sa.Column("funding", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_user_awards_funding_is_known"),
        TABLE,
        "funding IN ('full', 'partial')",
    )


def downgrade() -> None:
    """Drop the column. What mentors entered is lost; nothing else read it."""
    op.execute("SET lock_timeout = '3s'")
    op.drop_constraint(op.f("ck_user_awards_funding_is_known"), TABLE, type_="check")
    op.drop_column(TABLE, "funding")
