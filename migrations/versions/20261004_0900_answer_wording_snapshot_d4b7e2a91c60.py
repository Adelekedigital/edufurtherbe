"""Keep each answer's question and option wording as the mentee saw it.

**Expand only.** Two nullable columns with no default are metadata-only on
PostgreSQL 11+, so no table rewrite and no long lock. Old code writes nothing
here and reads nothing here, so it is safe beside either version.

**No backfill.** The wording a mentee saw before this revision is not recorded
anywhere, so there is nothing faithful to copy in; existing rows stay null and
readers fall back to the question's current wording (#350).

Revision ID: d4b7e2a91c60
Revises: c3e8a1f4b927
Create Date: 2026-10-04 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4b7e2a91c60"
down_revision: str | Sequence[str] | None = "c3e8a1f4b927"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.add_column("intake_answers", sa.Column("question_text", sa.Text(), nullable=True))
    op.add_column("intake_answers", sa.Column("option_text", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drops the snapshots; the wording they held is not recovered."""
    op.execute("SET lock_timeout = '3s'")
    op.drop_column("intake_answers", "option_text")
    op.drop_column("intake_answers", "question_text")
