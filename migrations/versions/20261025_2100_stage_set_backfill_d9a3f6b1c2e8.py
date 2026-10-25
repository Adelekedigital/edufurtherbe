"""Every staged session type has its stage in the set (decision 293).

**Data only**, ahead of the code that stops reading
``session_types.application_stage`` as a fallback. ``e0d7461f13ce`` copied the
column into ``session_type_stages`` once; a type staged only in the column
since (code from before the set during a deploy, or ``b8d2e6f4a3c7`` step 5,
which kept a remapped column for a type with no set rows) still reads its stage
through the fallback. This gives each such type its one row, at position 0, so
the set alone reads the same.

A type that already has rows is not touched, nor is one with no stage. A
soft-deleted type is filled like any other, because the fallback read did not
filter on deletion either. Safe in either deploy order: the code running now
prefers the rows, and they hold what the fallback read. **`ON CONFLICT DO
NOTHING`** because the app keeps serving: a mentor saving a set between the
`NOT EXISTS` and the insert wins, as their write would have.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d9a3f6b1c2e8"
down_revision: str | Sequence[str] | None = "c4e7a9b2d5f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO session_type_stages (session_type_id, stage, position) "
        "SELECT t.id, t.application_stage, 0 FROM session_types t "
        "WHERE t.application_stage IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM session_type_stages s WHERE s.session_type_id = t.id) "
        "ON CONFLICT DO NOTHING"
    )


def downgrade() -> None:
    """Nothing to undo.

    The previous revision reads these rows exactly as it read the column, so
    the data needs no change; a backfilled row cannot be told from one a mentor
    wrote, so none is deleted.
    """
