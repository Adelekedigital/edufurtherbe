"""A self-paused mentor's return date, and when its reminder went out.

Calendar request item 1, 2026-10-01. **Expand only**: two nullable columns on
`mentor_profiles`, metadata-only on PostgreSQL 11+, with no rewrite and nothing
for old code to read or write. On the profile rather than on the pause event,
because the event log is append-only and a return date is the *current* pause's
mutable state: changing it must not append an event.

**Downgrade** drops both. A paused mentor stays paused and loses only the date
and whether it was reminded.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7c41e9a2d58"
down_revision: str | Sequence[str] | None = "f3a91d2c7b45"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "mentor_profiles"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.add_column(TABLE, sa.Column("return_on", sa.Date(), nullable=True))
    op.add_column(
        TABLE, sa.Column("return_reminded_at", sa.TIMESTAMP(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.drop_column(TABLE, "return_reminded_at")
    op.drop_column(TABLE, "return_on")
