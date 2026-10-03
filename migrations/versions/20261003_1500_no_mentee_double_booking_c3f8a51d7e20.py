"""A mentee cannot hold two overlapping live sessions (#342).

The mentee's twin of `sessions_no_mentor_double_booking` (d7c31f8a2b45): the
same `session_window` range over the same live statuses, keyed on `mentee_id`.
The booking path refuses an overlap first with a clean 409; this is the wall a
race cannot get past.

**Fails loudly on existing overlaps, and never skips.** Before adding the
constraint it looks for live sessions that already overlap for one mentee and,
if any exist, stops with their ids. Adding the constraint would fail on them
anyway, with an error naming neither row; and silently leaving the constraint
off would ship a guarantee that does not hold. Which of two overlapping
sessions to keep is a decision about real bookings, so it is made by a person
before this runs. Development had none on 2026-10-03.

Rule 10 does not apply: no table is created.

Revision ID: c3f8a51d7e20
Revises: b7c41e9a2d58
Create Date: 2026-10-03 15:00:00.000000

"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3f8a51d7e20"
down_revision: str | Sequence[str] | None = "b7c41e9a2d58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "sessions_no_mentee_double_booking"

#: Duplicated from `app.infra.db.models.sessions.LIVE_STATUSES`, deliberately:
#: no migration imports from `app`. Pinned by
#: `test_the_live_status_predicate_has_one_meaning`, which finds every copy.
LIVE_STATUSES = "status IN ('pending_mentor_approval', 'confirmed')"


def _find_overlaps() -> sa.Select[Any]:
    """Live sessions that already overlap for one mentee, as id pairs."""
    sessions = sa.table(
        "sessions",
        sa.column("id"),
        sa.column("mentee_id"),
        sa.column("starts_at"),
        sa.column("duration_minutes"),
    )
    live = (
        sa.select(
            sessions.c.id, sessions.c.mentee_id, sessions.c.starts_at, sessions.c.duration_minutes
        )
        .where(sa.text(LIVE_STATUSES))
        .subquery()
    )
    a, b = live.alias("a"), live.alias("b")

    def window(side: Any) -> sa.ColumnElement[Any]:
        return sa.func.session_window(side.c.starts_at, side.c.duration_minutes)

    return (
        sa.select(a.c.mentee_id, a.c.id, b.c.id)
        .join_from(a, b, sa.and_(a.c.mentee_id == b.c.mentee_id, a.c.id < b.c.id))
        .where(window(a).op("&&")(window(b)))
        .order_by(a.c.mentee_id, a.c.id)
        .limit(20)
    )


ADD_CONSTRAINT = f"""
ALTER TABLE sessions
  ADD CONSTRAINT {CONSTRAINT}
  EXCLUDE USING gist (
    mentee_id WITH =,
    session_window(starts_at, duration_minutes) WITH &&
  ) WHERE ({LIVE_STATUSES})
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("SET lock_timeout = '5s'")
    op.execute("SET statement_timeout = '60s'")

    overlaps = op.get_bind().execute(_find_overlaps()).all()
    if overlaps:
        listed = "; ".join(f"mentee {m}: {a} and {b}" for m, a, b in overlaps)
        raise RuntimeError(
            f"{CONSTRAINT} cannot be added: live sessions already overlap for one "
            f"mentee ({listed}). Decide which to keep before migrating."
        )

    op.execute(ADD_CONSTRAINT)


def downgrade() -> None:
    """Downgrade schema. `session_window` stays: the mentor constraint uses it."""
    op.execute(f"ALTER TABLE sessions DROP CONSTRAINT {CONSTRAINT}")
