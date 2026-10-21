"""Add ``sessions.measured_minutes`` (Integrations PRD, 2026-10-21, decision 4).

The minutes the mentor and the mentee were in the room together, measured from
Daily's records once an EduFurther video session's call is over. **Expand
only**: nullable, no default and no backfill, so the add is a catalogue change
with no table rewrite. Null is "not measured" — every existing row, Meet, and
migrated sessions — and the stats then count the booked length, which is
exactly today's figure.

The ``CHECK`` holds the one invariant a minute count has. Every existing value
is null, so validating it reads the table without finding anything to refuse.

The downgrade drops the column: what it held is derivable again from Daily's
records for a day after each call and is a refinement of a figure the booked
length still gives.

Revision ID: c3e8a1f5d29b
Revises: b2d6f9a1c4e7
Create Date: 2026-10-21 22:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3e8a1f5d29b"
down_revision: str | Sequence[str] | None = "b2d6f9a1c4e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Bare for `op`, which adds the `ck_sessions_` prefix; the rendered name is
#: what the database holds, declared so the identifier test finds it verbatim.
CHECK = "measured_minutes_not_negative"
CHECK_RENDERED = "ck_sessions_measured_minutes_not_negative"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.add_column("sessions", sa.Column("measured_minutes", sa.Integer(), nullable=True))
    op.create_check_constraint(
        CHECK, "sessions", "measured_minutes IS NULL OR measured_minutes >= 0"
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.drop_constraint(CHECK, "sessions", type_="check")
    op.drop_column("sessions", "measured_minutes")
