"""Record who wrote each conferencing option: the legacy load or the mentor.

**Expand only.** A `NOT NULL` column with a constant default is metadata-only on
PostgreSQL 11+, so no table rewrite and no long lock; every existing row reads
`import`, which is true: until `/me/conferencing` (#224) nothing but the load and
the seeding migration wrote this table. The new code writes `mentor` on a PATCH;
old code writes nothing here, so it is safe beside either.

A revision of its own, separate from the cleanup `a7c4e2d91f3b` that reads it,
so that downgrading the cleanup keeps the provenance: a downgrade-then-upgrade
of the cleanup then still knows which Meet rows the mentor chose.

Revision ID: 5e8d1b7c3a29
Revises: f3a91d2c7b45
Create Date: 2026-10-01 11:50:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5e8d1b7c3a29"
down_revision: str | Sequence[str] | None = "f3a91d2c7b45"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECK = "ck_mentor_conferencing_options_source_is_known"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.add_column(
        "mentor_conferencing_options",
        sa.Column("source", sa.Text(), nullable=False, server_default=sa.text("'import'")),
    )
    # `op.f`: the name is final, so the naming convention must not prefix it again.
    op.create_check_constraint(
        op.f(CHECK), "mentor_conferencing_options", "source IN ('import', 'mentor')"
    )


def downgrade() -> None:
    """Drops the provenance; a later upgrade backfills every row as `import`."""
    op.execute("SET lock_timeout = '3s'")
    op.drop_constraint(op.f(CHECK), "mentor_conferencing_options", type_="check")
    op.drop_column("mentor_conferencing_options", "source")
