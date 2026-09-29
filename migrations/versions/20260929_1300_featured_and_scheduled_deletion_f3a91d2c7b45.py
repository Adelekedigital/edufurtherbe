"""A featured session type per mentor, and deletion scheduled behind booked sessions.

Settled decisions #217 and #218, Session Types frontend round 4. **The expand
step**, and all of it additive.

* ``session_types.is_featured``, ``NOT NULL DEFAULT false`` — metadata-only on
  PostgreSQL 11+, so no rewrite; every existing offering starts un-featured,
  which is what it was.
* ``ix_session_types_one_featured``: at most one featured, live offering per
  mentor. Built on an all-false column, so it indexes no row yet.
* ``ck_session_types_featured_is_active``: only a shown offering is featured.
* ``session_types.deletion_scheduled_at``, nullable: when a mentor asked for
  an offering with booked sessions to go.
* ``ck_session_types_scheduled_deletion_is_hidden``: a scheduled offering is
  hidden until it goes or is restored.

The two `CHECK`s are added ``NOT VALID`` then validated. Every existing row
satisfies both (the columns are new and all-default), so the scan finds
nothing; the split keeps the `ACCESS EXCLUSIVE` window to the metadata change.

**Rolling deploy:** old code neither reads nor writes either column, and the
defaults satisfy both constraints, so an old pod's inserts and updates pass.
The one exposure is an old pod **showing** an offering the new code scheduled
(`PATCH {"is_active": true}`): the `CHECK` refuses it as an integrity error,
a 500, for the length of one swap. Accepted on the same terms as #216's.

**Downgrade** drops both columns and the constraints. A scheduled offering
stays hidden and undeleted — the old code refuses its `DELETE` with the `409`
again, which is where it was — and which offering was featured is lost, which
only ever changed an order.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3a91d2c7b45"
down_revision: str | Sequence[str] | None = "228315723d63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "session_types"
FEATURED_INDEX = "ix_session_types_one_featured"
FEATURED_CHECK = "ck_session_types_featured_is_active"
SCHEDULED_CHECK = "ck_session_types_scheduled_deletion_is_hidden"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.add_column(
        TABLE,
        sa.Column("is_featured", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        TABLE, sa.Column("deletion_scheduled_at", sa.TIMESTAMP(timezone=True), nullable=True)
    )
    op.execute(
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {FEATURED_CHECK} "
        "CHECK (NOT is_featured OR is_active) NOT VALID"
    )
    op.execute(
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {SCHEDULED_CHECK} "
        "CHECK (deletion_scheduled_at IS NULL OR NOT is_active) NOT VALID"
    )
    op.execute(f"ALTER TABLE {TABLE} VALIDATE CONSTRAINT {FEATURED_CHECK}")
    op.execute(f"ALTER TABLE {TABLE} VALIDATE CONSTRAINT {SCHEDULED_CHECK}")
    op.create_index(
        FEATURED_INDEX,
        TABLE,
        ["mentor_user_id"],
        unique=True,
        postgresql_where=sa.text("is_featured AND deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.drop_index(FEATURED_INDEX, table_name=TABLE)
    op.drop_constraint(op.f(SCHEDULED_CHECK), TABLE, type_="check")
    op.drop_constraint(op.f(FEATURED_CHECK), TABLE, type_="check")
    op.drop_column(TABLE, "deletion_scheduled_at")
    op.drop_column(TABLE, "is_featured")
