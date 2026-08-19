"""One reminder per session per kind, whatever QStash does.

**QStash retries, and a retried callback would queue a second copy.** The
callback re-reads the session and enqueues a message; run twice it enqueues
twice, and the second one is an identical email a mentor did not need. Nothing
in the outbox stops that, because two rows differing only by id are exactly what
the table is normally for — a message per recipient.

So the invariant is declared where it can be enforced: **at most one pending or
sent reminder per session per kind.** ADR 0015 admits no composite primary key
and prescribes this shape for exactly this case — a natural key re-declared as
``UNIQUE`` — so the index is the sanctioned form rather than a workaround.

**Partial on the reminder shape**, so it constrains nothing else. Every other
message writes no ``kind``, and a partial index skips those rows entirely — a
mentor can still be told a session was booked *and* cancelled, which a full
index over ``(entity_id, event_type)`` would have permitted anyway but which is
worth being explicit about.

**Includes `failed` and `skipped` deliberately.** A reminder that has run out of
attempts must not be re-queued by a late callback: it was owed, it was tried,
and the record of that is the point. Only a *deleted* row frees the slot, and
nothing deletes.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e8c1d306b4a7"
down_revision: str | Sequence[str] | None = "d7b2c04e9f31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """One partial unique index. Nothing existing changes shape."""
    # Written as raw SQL rather than through `op.create_index`, because the
    # expression indexes a JSONB path and the operator form is clearer read
    # aloud than the equivalent `sa.text` inside a column list.
    op.execute(
        "CREATE UNIQUE INDEX uq_outbox_events_reminder "
        "ON outbox_events (entity_id, event_type, (payload ->> 'kind')) "
        "WHERE payload ? 'kind'"
    )


def downgrade() -> None:
    """Drops the constraint, and with it the only thing stopping a retried
    callback queueing a duplicate reminder."""
    op.execute("DROP INDEX IF EXISTS uq_outbox_events_reminder")
