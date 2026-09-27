"""``featured_mentors.source`` and ``chosen_by``: an admin may choose a week.

Settled decision #188. The automatic rotation stays the default (#177); an
admin's override is a row like any other, marked ``source = 'admin'`` and
naming the admin in ``chosen_by``. The week's mentor is already "the newest
still-bookable row of the week", so the reader needs no change.

- ``source``: ``automatic`` or ``admin``, text + CHECK (#100), defaulted so
  every existing row — all written by the rotation — reads ``automatic``.
- ``chosen_by``: the admin, ``RESTRICT`` like every other attribution column.
- ``admin_names_its_admin``: ``chosen_by`` is set for exactly the admin rows.

Additive: a nullable column and a defaulted one, metadata-only on PostgreSQL
11+, and the CHECKs hold for every existing row (automatic, no admin), so both
code versions serve during the deploy.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "58725a5c5ed8"
down_revision: str | Sequence[str] | None = "97e5d09cc9cb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "featured_mentors"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.add_column(
        TABLE,
        sa.Column("source", sa.Text(), server_default=sa.text("'automatic'"), nullable=False),
    )
    op.add_column(TABLE, sa.Column("chosen_by", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_featured_mentors_chosen_by_users"),
        TABLE,
        "users",
        ["chosen_by"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_featured_mentors_source_is_known"),
        TABLE,
        "source IN ('automatic', 'admin')",
    )
    op.create_check_constraint(
        op.f("ck_featured_mentors_admin_names_its_admin"),
        TABLE,
        "(source = 'admin') = (chosen_by IS NOT NULL)",
    )


def downgrade() -> None:
    """Drop both columns. Admin rows stay as rows but lose who chose them —
    they then read as the rotation's own picks, which is the pre-#188 world."""
    op.execute("SET lock_timeout = '3s'")
    op.drop_constraint(op.f("ck_featured_mentors_admin_names_its_admin"), TABLE, type_="check")
    op.drop_constraint(op.f("ck_featured_mentors_source_is_known"), TABLE, type_="check")
    op.drop_constraint(op.f("fk_featured_mentors_chosen_by_users"), TABLE, type_="foreignkey")
    op.drop_column(TABLE, "chosen_by")
    op.drop_column(TABLE, "source")
