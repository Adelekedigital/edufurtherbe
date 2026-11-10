"""``outbox_events.destination`` loses its ``'posthog'`` default (PRD 30 PR 1).

**A divergence from the canonical package**, which defaults ``destination`` to
``'posthog'``. A row that named no destination was read by the email drain as a
message (it selected every row but the push rows); each drain now selects its
own destinations, and analytics rows name ``'analytics'``. Every writer already
sets the column (`outbox.enqueue`, `outbox.enqueue_analytics`), so dropping the
default leaves a forgotten one failing ``NOT NULL`` at the insert instead of
being swept as the wrong thing.

No ``CHECK`` lists the destinations, so nothing to widen.

Expand only: running code always names a destination, so it is untouched.
Downgrade restores the default.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2a9c4f7b153"
down_revision: str | Sequence[str] | None = "c3f81a5d27e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("outbox_events", "destination", server_default=None)


def downgrade() -> None:
    op.alter_column("outbox_events", "destination", server_default="posthog")
