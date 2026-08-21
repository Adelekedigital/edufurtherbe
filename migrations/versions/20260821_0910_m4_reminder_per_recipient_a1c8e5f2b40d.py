"""One reminder per session per kind **per recipient**.

`uq_outbox_events_reminder` was ``(entity_id, event_type, payload->>'kind')``,
which is right for a message with one recipient and wrong for one with two.

**It was correct when written and became wrong when a second message used it.**
The only reminder at the time was the mentor's response nudge, which goes to the
mentor alone — so the index never had two rows to tell apart. Pre-session
reminders go to *both* parties, and the outbox deliberately writes one row per
recipient so each can succeed or fail on its own. Under the old index the
second row conflicted with the first and `ON CONFLICT DO NOTHING` dropped it in
silence: one party reminded, the other not, and nothing anywhere saying so.

Found by a test asserting two rows and getting one.

**Additive in the sense that matters**: the constraint gets *narrower*, so every
row the old index permitted the new one permits too. No existing row can violate
it — two rows agreeing on session, kind and recipient could not have existed,
because the old index already forbade a superset of that.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c8e5f2b40d"
down_revision: str | Sequence[str] | None = "f4a83b1e07d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAME = "uq_outbox_events_reminder"
TABLE = "outbox_events"
WHERE = "payload ? 'kind'"


def upgrade() -> None:
    """Replace the index with one that also distinguishes the recipient."""
    op.drop_index(NAME, table_name=TABLE)
    op.create_index(
        NAME,
        TABLE,
        [
            "entity_id",
            "event_type",
            sa.text("(payload ->> 'kind')"),
            # **The recipient, which is what was missing.** The outbox writes one
            # row per person so a send that fails for one is retried for that
            # one; an index blind to the recipient collapses those rows back
            # together and loses everybody after the first.
            sa.text("(payload ->> 'recipient_id')"),
        ],
        unique=True,
        postgresql_where=sa.text(WHERE),
    )


def downgrade() -> None:
    """Back to the narrower key, which is lossy if two recipients exist.

    A session with reminders queued for both parties cannot satisfy the old
    index, so this fails rather than silently deleting one of them. That is the
    honest behaviour: the rows are owed messages, and choosing one to discard is
    not a migration's decision to make.
    """
    op.drop_index(NAME, table_name=TABLE)
    op.create_index(
        NAME,
        TABLE,
        ["entity_id", "event_type", sa.text("(payload ->> 'kind')")],
        unique=True,
        postgresql_where=sa.text(WHERE),
    )
