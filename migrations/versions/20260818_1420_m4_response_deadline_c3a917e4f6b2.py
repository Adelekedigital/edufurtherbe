"""``sessions.respond_by`` — when an unanswered request dies.

**A stored column with a sweep, not a derived value**, and the reason is the
exclusion constraint. ``sessions_no_mentor_double_booking`` is an ``EXCLUDE``
over ``LIVE_STATUSES``, which includes ``pending_mentor_approval`` — so a request
nobody answered **holds the mentor's hour indefinitely**. Nothing writes to an
abandoned row, so a trigger cannot free it, and folding the deadline into the
constraint's predicate is not possible at all: PostgreSQL requires those to be
``IMMUTABLE`` and ``now()`` is ``STABLE``.

**``respond_by = starts_at - 6 hours``, measured backwards from the session.**
The guarantee is to the *mentee*: you will know before the session, early enough
for the answer to be useful. A window measured forward from ``created_at``
guarantees them nothing.

**Six hours rather than twenty-four, and the floor is what decides it.** The
mentor's time to answer is ``(starts_at - booked_at) - W``, and ``booked_at`` is
at best ``starts_at - min_notice_minutes``. Against the 24-hour notice floor a
``W`` of 24h leaves **zero** — every request on a default-configured offering
would expire the instant it was made. At six hours the same mentor has eighteen,
and a 72-hour notice gives sixty-six.

**Null on auto-confirming offerings, and that is the domain rule rather than a
default.** Nothing is awaiting an answer there, so there is no window to elapse;
the partial index below is predicated on the status for the same reason.

**Not backfilled.** Every migrated session is terminal or was booked under a
lifecycle that had no response window, so inventing a deadline for them would
manufacture expiries for sessions nobody is waiting on. The column is null on
every existing row and is written only by bookings made after this ships.

**Reminders are not in this migration.** The schedule is settled — on booking,
24 hours before ``respond_by`` where the lead allows it, and 12 hours before —
and it needs a notification channel that does not exist. The deadline and its
producer ship without them, because freeing the slot is the half that does not
depend on being able to tell anybody.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3a917e4f6b2"
down_revision: str | Sequence[str] | None = "b7f0e2c94a13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """One nullable column and one partial index. Nothing existing changes shape."""
    op.add_column(
        "sessions",
        sa.Column("respond_by", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    # **Partial, on the one status that can expire.** The sweep asks for pending
    # requests past their deadline and nothing else ever filters on this column,
    # so an index over the whole table would be mostly rows the query discards —
    # and `alembic check` cannot compare a predicate, which is why a test
    # asserts it against `pg_indexes`.
    op.create_index(
        "ix_sessions_pending_respond_by",
        "sessions",
        ["respond_by"],
        postgresql_where=sa.text("status = 'pending_mentor_approval'"),
    )


def downgrade() -> None:
    """Drops the deadline, and with it the only thing that frees an abandoned
    request's slot. The sweep goes in the same release."""
    op.drop_index("ix_sessions_pending_respond_by", table_name="sessions")
    op.drop_column("sessions", "respond_by")
