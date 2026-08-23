"""The interval's index drops the column the interval stopped using.

``ix_reviews_author_subject_created`` was ``(reviewed_by, reviewed_for,
created_at DESC)``, shaped for a window scoped to a **mentor**. That window is
scoped to an **offering** now, and the offering is reached by joining
``sessions`` — so the predicate on ``reviews`` is ``reviewed_by`` plus
``created_at``, and nothing has read ``reviewed_for`` since.

**Measured before being written, and the obvious justification turned out to be
wrong.** The expectation was that a gap in the middle would stop ``created_at``
serving as a range bound. It does not: on 25,073 reviews both shapes plan as an
*Index Only Scan* with ``Heap Fetches: 0`` and both carry ``created_at`` in the
``Index Cond``. PostgreSQL evaluates the non-contiguous column against the index
tuple rather than falling back to the heap.

What the dead column actually costs is **size, and therefore every write**:

    (reviewed_by, reviewed_for, created_at)   1448 kB
    (reviewed_by, created_at)                 1000 kB     31% smaller

448 kB over 25,073 rows, maintained on every insert, for a column no predicate
reads. That is the reason this migration exists — not a plan that needed fixing.

The name changes with the shape. ``author_subject_created`` describes a rule the
product no longer has, and an index whose name states the wrong invariant is
worse than a wide one: the next reader trusts it.

**Additive in the sense that matters.** An index carries no data, so dropping one
loses nothing and re-creating it is the whole of ``downgrade``. Both statements
are safe on a populated table; neither can fail on existing rows.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9e4b1f78d02"
down_revision: str | Sequence[str] | None = "b6d2f0a794c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "reviews"
OLD = "ix_reviews_author_subject_created"
NEW = "ix_reviews_author_created"


def upgrade() -> None:
    """Replace the three-column index with the two columns the predicate reads."""
    op.drop_index(OLD, table_name=TABLE)
    op.create_index(NEW, TABLE, ["reviewed_by", sa.text("created_at DESC")])


def downgrade() -> None:
    """Back to the wider shape. Lossless — an index holds no rows of its own."""
    op.drop_index(NEW, table_name=TABLE)
    op.create_index(
        OLD,
        TABLE,
        ["reviewed_by", "reviewed_for", sa.text("created_at DESC")],
    )
