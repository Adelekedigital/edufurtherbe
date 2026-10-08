"""When a party was first seen in the room: ``session_participants.in_room_at``.

#382, owner 2026-10-08. For an EduFurther video (Daily) session, attendance is
to come from what the provider saw, Daily's ``participant.joined`` webhook keyed
on the ``user_id`` minted into each meeting token, instead of from a press of
Join. This column is where that observation lands.

**Beside ``joined_at``, not instead of it.** ``joined_at`` keeps its meaning,
the first Join press, because the frontend gates re-entry on it. A party who
pressed Join in time but whose call never connected (a blocked popup, a crash,
a slow webhook) must not be locked out as a late first-timer. Two facts, two
columns: the press decides entry, and presence decides attendance.

**Nullable with no backfill, and null is permanent for older rows.** Nothing
observed presence before this, so there is nothing to backfill from. Null also
stays the answer for Google Meet and custom venues, which give no presence
signal.

Expand only, and merged alone (project-conventions, *Migration ordering*):
nothing reads the column until the next PR, so this lands inert. Adding a
nullable column takes a brief ``ACCESS EXCLUSIVE`` lock and rewrites nothing.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3e9d1a7c452"
down_revision: str | Sequence[str] | None = "e1a7c3b94f28"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "session_participants"
COLUMN = "in_room_at"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.add_column(TABLE, sa.Column(COLUMN, sa.TIMESTAMP(timezone=True), nullable=True))


def downgrade() -> None:
    """Drop the column.

    What it held is lost: an observation of presence cannot be re-read from
    our side. Daily keeps its own meeting records for a time, which is the only
    way back.
    """
    op.drop_column(TABLE, COLUMN)
