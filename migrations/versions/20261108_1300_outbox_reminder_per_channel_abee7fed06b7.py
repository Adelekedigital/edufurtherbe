"""One reminder per session per kind per recipient **per channel** (decision 312).

`uq_outbox_events_reminder` was ``(entity_id, event_type, kind, recipient_id)``.
A push row for the five-minute reminder agrees with its email row on all four,
so ``ON CONFLICT DO NOTHING`` dropped the push in silence. ``destination`` joins
the key.

**Narrower, so additive**: every row the old index allowed, this one allows,
and no existing row can violate it. Running code is unaffected: its insert
names no conflict target, so any unique violation is still a no-op for it.
Merged alone, before the code that writes push rows (project-conventions,
*Migration ordering*).

**Swapped concurrently**, as `7b3e91c4a2d6` does: built under a temporary name,
the old index dropped, then renamed. `outbox_events` keeps every row it has
written, so a plain rebuild would hold the table's write lock for the build,
and every enqueue and drain with it. There is no moment without the index.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "abee7fed06b7"
down_revision: str | Sequence[str] | None = "5b2e9d41c7a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "outbox_events"
INDEX = "uq_outbox_events_reminder"
BUILDING = "uq_outbox_events_reminder_next"
KEY = "entity_id, event_type, (payload ->> 'kind'), (payload ->> 'recipient_id')"
WHERE = "payload ? 'kind'"


def _swap_index(columns: str) -> None:
    """Replace the index with one over ``columns``, never going without.

    ``IF EXISTS`` throughout: a failed ``CONCURRENTLY`` build leaves an
    ``INVALID`` index behind, and a re-run must get past it.
    """
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '3s'")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {BUILDING}")
        op.execute(
            f"CREATE UNIQUE INDEX CONCURRENTLY {BUILDING} ON {TABLE} ({columns}) WHERE {WHERE}"
        )
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
        op.execute(f"ALTER INDEX {BUILDING} RENAME TO {INDEX}")


def upgrade() -> None:
    _swap_index(f"{KEY}, destination")


def downgrade() -> None:
    """Back to the key without the channel. **Fails, deleting nothing**, if a
    reminder is queued on two channels: the narrower build refuses, and the
    wider index stays. Both rows are owed messages."""
    _swap_index(KEY)
