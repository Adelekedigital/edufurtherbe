"""``mentor_next_availability.next_available_session_type_id``.

Settled decision #189 (frontend request #18). The refresh job already finds
the earliest slot per offering and keeps the first; this stores *which*
offering it was, so "Book {time}" opens on it.

Nullable, and null exactly when ``next_available_at`` is. ``ON DELETE SET
NULL``: a deleted offering must not take the cached row with it, and the next
refresh rewrites the value. No index: nothing looks rows up by it, and session
types are soft-deleted, so the ``SET NULL`` path is all but unused.

Additive and rolling-deploy safe: the old job's upsert names only its own
columns, so while it still runs the new column simply stays null — and a null
id is what the API already sends whenever it cannot vouch for the value.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d41c7e9a2b85"
down_revision: str | Sequence[str] | None = "58725a5c5ed8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "mentor_next_availability"
COLUMN = "next_available_session_type_id"
FK = "fk_mentor_next_availability_session_type_id"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.add_column(TABLE, sa.Column(COLUMN, sa.Uuid(), nullable=True))
    op.create_foreign_key(FK, TABLE, "session_types", [COLUMN], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    """Drop the column. It is a cache; the job keeps working without it."""
    op.execute("SET lock_timeout = '3s'")
    op.drop_constraint(FK, TABLE, type_="foreignkey")
    op.drop_column(TABLE, COLUMN)
