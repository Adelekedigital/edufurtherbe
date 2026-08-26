"""One monthly grant per user per period.

**A scheduled job runs twice.** A retry, a manual trigger beside the cron, an
operator checking it works — and without this the second run hands every
unlocked mentee another three credits. Nothing downstream would notice: the
balance is a ``SUM``, and it would simply be right about the wrong number.

**Keyed on ``(user_id, expires_at)``, which is the period.** Every monthly lot
granted in one month shares an expiry — the 1st of the next month at midnight
UTC — so that pair is exactly "once this month". Two things follow:

- A job that fires late, on the 3rd, still collides with the one that fired on
  the 1st. The guard keys on the period rather than on the day the job ran.
- ``date_trunc('month', created_at)`` is avoided, which matters: on a
  ``timestamptz`` it is ``STABLE`` rather than ``IMMUTABLE`` — it depends on the
  session's ``TimeZone`` — so PostgreSQL refuses it in an index without an
  explicit ``AT TIME ZONE``. The expiry column carries the same information and
  is already there.

**Partial on the source**, for the reason the starter's index is: on
``(user_id, expires_at)`` alone, a refund and a monthly grant that happened to
share an expiry would collide, and a user would silently lose one.

Additive: no ``monthly_free`` lot exists yet, because the job that writes them
ships in this same PR.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a91e37c4d820"
# Off `main`'s head. The review-moderation stack also branches from here, so
# whichever of the two merges second fixes its `down_revision` at merge time —
# Alembic's chain is linear and two heads make `upgrade head` fail outright
# (how-we-work rule 1).
down_revision: str | Sequence[str] | None = "f2a71d64c9e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LOTS = "credit_lots"
INDEX = "uq_credit_lots_one_monthly_grant_per_period"


def upgrade() -> None:
    op.create_index(
        INDEX,
        LOTS,
        ["user_id", "expires_at"],
        unique=True,
        postgresql_where=sa.text("source = 'monthly_free'"),
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name=LOTS)
