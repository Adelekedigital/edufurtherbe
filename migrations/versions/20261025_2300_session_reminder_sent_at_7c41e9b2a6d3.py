"""Add ``sessions.reminder_sent_at`` (Mentee Bookings PRD, decision 294).

When the mentee reminded the mentor about a pending request — at most once per
session. **Expand only**: nullable, no default and no backfill, so the add is a
catalogue change with no table rewrite. Null is "no reminder sent", which is
true of every existing row. Shipped alone, ahead of the endpoint that writes
it and the session read that publishes it (*Migration ordering*).

The downgrade drops the column; it only loses which requests were already
nudged, so a mentee could nudge one again.

Revision ID: 7c41e9b2a6d3
Revises: d9a3f6b1c2e8
Create Date: 2026-10-25 23:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c41e9b2a6d3"
down_revision: str | Sequence[str] | None = "d9a3f6b1c2e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.add_column(
        "sessions", sa.Column("reminder_sent_at", sa.TIMESTAMP(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.drop_column("sessions", "reminder_sent_at")
