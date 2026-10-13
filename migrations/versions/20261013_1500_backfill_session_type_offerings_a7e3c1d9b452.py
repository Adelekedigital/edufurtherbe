"""Backfill ``session_type_offerings`` from the legacy column, again (#274).

``ceb7410fb394`` backfilled every classified type when the set table arrived
(#266). Since then the API has dual-written both, but the demo seed has written
only ``session_types.service_offering_id``, so a type it made has its offering
in the column alone, and reads show it only through the legacy fallback. The
contract step removes that fallback and then the column, so every such type is
given its row first, or it would read as unclassified.

**Only types with no set at all**, at position 0, as the first backfill did. A
type that has a set already keeps it untouched: the API wrote it, and it is the
truth the column was only a copy of.

Data only, expand-safe and re-runnable: nothing reads differently afterwards,
because the fallback already showed exactly this row. Merged alone, before the
code that drops the fallback (project-conventions, *Migration ordering*).
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7e3c1d9b452"
down_revision: str | Sequence[str] | None = "5d8b3e1f7a24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.execute(
        "INSERT INTO session_type_offerings (session_type_id, service_offering_id, position) "
        "SELECT t.id, t.service_offering_id, 0 FROM session_types t "
        "WHERE t.service_offering_id IS NOT NULL "
        "AND NOT EXISTS ("
        "SELECT 1 FROM session_type_offerings o WHERE o.session_type_id = t.id)"
    )


def downgrade() -> None:
    """Nothing to undo.

    The rows it added say what the column already said, and code at the
    previous revision reads them exactly as it read the column through its
    fallback, so leaving them is the faithful state. Deleting them would need to
    know which rows this added, which nothing records.
    """
