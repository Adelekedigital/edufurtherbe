"""One reminder per session per kind per recipient **per channel** (decision 312).

`uq_outbox_events_reminder` was ``(entity_id, event_type, kind, recipient_id)``.
A push row for the five-minute reminder agrees with its email row on all four,
so ``ON CONFLICT DO NOTHING`` dropped the push in silence. ``destination`` joins
the key.

**Narrower, so additive**: every row the old index allowed, this one allows,
and no existing row can violate it. Running code is unaffected: its inserts
name no conflict target, so any unique violation is still a no-op for it.
Merged alone, before the code that writes push rows (project-conventions,
*Migration ordering*).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "abee7fed06b7"
down_revision: str | Sequence[str] | None = "5b2e9d41c7a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAME = "uq_outbox_events_reminder"
TABLE = "outbox_events"
WHERE = "payload ? 'kind'"
KEY = [
    "entity_id",
    "event_type",
    sa.text("(payload ->> 'kind')"),
    sa.text("(payload ->> 'recipient_id')"),
]


def _replace(columns: list[str | sa.TextClause]) -> None:
    op.execute("SET lock_timeout = '3s'")
    op.drop_index(NAME, table_name=TABLE)
    op.create_index(NAME, TABLE, columns, unique=True, postgresql_where=sa.text(WHERE))


def upgrade() -> None:
    _replace([*KEY, "destination"])


def downgrade() -> None:
    """Back to the key without the channel. Fails, rather than deleting a row,
    if a reminder is queued on two channels: both are owed messages."""
    _replace(KEY)
