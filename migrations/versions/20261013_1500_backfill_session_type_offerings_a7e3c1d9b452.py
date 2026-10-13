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

**Where a set exists but its first item differs from the column**, the set is
left as it is. Reads already take the set over the column, so that is what
every client sees today; dropping the column changes nothing visible. Only a
demo re-seed could cause the mismatch (it updated the column alone), and the
seed now writes the set itself (#439).

**Soft-deleted and scheduled-for-deletion types are included**: this is a
faithful copy of what each row says, and every reader of the set already
filters on its parent type, as it filtered the column.

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
    # **Writers wait for this one statement.** Without it, an API write that
    # gives a column-only type its set after this statement's snapshot, with a
    # different offering, lands beside the stale column value, both at
    # position 0. `SHARE ROW EXCLUSIVE` blocks inserts and deletes on the set
    # (and itself) but not reads, and lasts only to the end of the migration's
    # transaction; the statement then sees every committed set.
    op.execute("LOCK TABLE session_type_offerings IN SHARE ROW EXCLUSIVE MODE")
    op.execute(
        "INSERT INTO session_type_offerings (session_type_id, service_offering_id, position) "
        "SELECT t.id, t.service_offering_id, 0 FROM session_types t "
        "WHERE t.service_offering_id IS NOT NULL "
        "AND NOT EXISTS ("
        "SELECT 1 FROM session_type_offerings o WHERE o.session_type_id = t.id) "
        # A set the API writes while this runs, committed after the statement's
        # snapshot, would otherwise abort the deploy on the pair index.
        "ON CONFLICT (session_type_id, service_offering_id) DO NOTHING"
    )


def downgrade() -> None:
    """Nothing to undo.

    The rows it added say what the column already said, and code at the
    previous revision reads them exactly as it read the column through its
    fallback, so leaving them is the faithful state. Deleting them would need to
    know which rows this added, which nothing records.
    """
