"""Mentor defaults for length and notice, and offerings that inherit them.

Settled decision #216, Session Types frontend round 3 B. **The expand step.**

* ``mentor_profiles`` gains ``default_duration_minutes`` and
  ``default_min_notice_minutes``, nullable: null means the platform's (60
  minutes, 24 hours). The CHECKs are the offering's own — validity for
  duration, sanity for notice (#104).
* ``session_type_booking_configs.duration_minutes`` and ``min_notice_minutes``
  drop ``NOT NULL``: null means *follow my default*. ``min_notice_minutes``
  keeps its server default, so code from before this release that inserts
  without naming it still gets the floor. **Metadata-only**; no row changes, so
  every existing offering keeps its own values and reads exactly as before.

**Rolling-deploy exposure, accepted for one container swap.** Once this runs,
either version can write a null during the swap: an **old** pod's
``PATCH /me/session-types/{id} {"duration_minutes": null}`` (old code dumped
it straight to the column, which used to refuse it), and a **new** pod's
``POST /me/session-types`` without ``min_notice_minutes`` (null means inherit
now). Old pods read the columns raw, so any such offering answers **500 on
``/slots`` and on the owner's ``/me/session-types``** from an old pod until the
swap finishes. Accepted by the owner's coordinator on 2026-09-29: there is no
production traffic yet, and the window is one swap. With traffic, this would
need a release that tolerates null before the one that writes it.

**The availability-change trigger on ``mentor_profiles`` gains the columns
slots now read** — these two, and ``booking_window_days`` /
``break_after_minutes``, which #204 began reading without adding (its updates
reached the card's next free time only through the max-age refresh). A test
now pins the trigger's columns to what `booking_rules` reads.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "228315723d63"
down_revision: str | Sequence[str] | None = "e0d7461f13ce"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DURATION_CHECK = "ck_mentor_profiles_default_duration_minutes_valid"
NOTICE_CHECK = "ck_mentor_profiles_default_min_notice_minutes_sane"

TRIGGER = "trg_log_availability_change"
#: The trigger's `UPDATE OF` list on `mentor_profiles`, before and after —
#: written out, as the migration that created it wrote them.
BEFORE = "user_id, approval_status, listing_status, deleted_at"
AFTER = (
    "user_id, approval_status, listing_status, deleted_at, booking_window_days, "
    "break_after_minutes, default_duration_minutes, default_min_notice_minutes"
)


def _watch(columns: str) -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {TRIGGER} ON mentor_profiles")
    op.execute(
        f"CREATE TRIGGER {TRIGGER} AFTER INSERT OR UPDATE OF {columns} OR DELETE "
        "ON mentor_profiles FOR EACH ROW "
        "EXECUTE FUNCTION log_availability_change('user_id', 'mentor')"
    )


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.add_column(
        "mentor_profiles", sa.Column("default_duration_minutes", sa.Integer(), nullable=True)
    )
    op.add_column(
        "mentor_profiles", sa.Column("default_min_notice_minutes", sa.Integer(), nullable=True)
    )
    op.create_check_constraint(
        op.f(DURATION_CHECK),
        "mentor_profiles",
        "default_duration_minutes IS NULL OR default_duration_minutes BETWEEN 5 AND 480",
    )
    op.create_check_constraint(
        op.f(NOTICE_CHECK),
        "mentor_profiles",
        "default_min_notice_minutes IS NULL OR default_min_notice_minutes BETWEEN 0 AND 43200",
    )
    op.alter_column("session_type_booking_configs", "duration_minutes", nullable=True)
    op.alter_column("session_type_booking_configs", "min_notice_minutes", nullable=True)
    _watch(AFTER)


def downgrade() -> None:
    """Restore `NOT NULL`, writing each inheriting offering its **resolved** value
    first, so every offering keeps the length and notice it had — then drop the
    mentor defaults. Faithful: nothing a mentee could book changes."""
    op.execute("SET lock_timeout = '3s'")
    _watch(BEFORE)
    op.execute(
        "UPDATE session_type_booking_configs c "
        "SET duration_minutes = COALESCE(c.duration_minutes, m.default_duration_minutes, 60), "
        "    min_notice_minutes = "
        "COALESCE(c.min_notice_minutes, m.default_min_notice_minutes, 1440) "
        "FROM session_types t JOIN mentor_profiles m ON m.user_id = t.mentor_user_id "
        "WHERE t.id = c.session_type_id "
        "AND (c.duration_minutes IS NULL OR c.min_notice_minutes IS NULL)"
    )
    op.alter_column("session_type_booking_configs", "min_notice_minutes", nullable=False)
    op.alter_column("session_type_booking_configs", "duration_minutes", nullable=False)
    op.drop_constraint(op.f(NOTICE_CHECK), "mentor_profiles", type_="check")
    op.drop_constraint(op.f(DURATION_CHECK), "mentor_profiles", type_="check")
    op.drop_column("mentor_profiles", "default_min_notice_minutes")
    op.drop_column("mentor_profiles", "default_duration_minutes")
