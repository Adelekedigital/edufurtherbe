"""Held starter lots die at the month end, like every credit (decision 308).

Data only. Before decision 308 the starter (`profile_completed`) never expired —
every writer, the API and the migration's loader alike, wrote it with
``expires_at IS NULL``. Under the credit levels it is a level-1 credit and dies
at the month end with the rest:

- **a held starter gets the end of the month this runs in**. The level grants
  count a lot as this period's when ``expires_at`` is the period's end, so it
  is not paid a second time by the next top-up — left ``NULL`` it would be a
  never-expiring extra beside every month's level;
- **a spent starter gets the end of the month it was granted in**, already
  past, so it is not mistaken for this period's payment. It still needs a date:
  a refund of it inherits ``NULL`` as "never expires" (`refund_expiry`).

The 1st's grant then pays each mentee their level as usual.

Production holds no starter yet (it is not live), so this moves staging rows
only. Idempotent: a second run finds no ``NULL`` starter.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8c4e1f7a2d93"
down_revision: str | Sequence[str] | None = "3f9c2a7e1b64"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.execute("SET statement_timeout = '30s'")
    # `domain.credits.end_of_month` in SQL: `date_trunc` on the UTC wall clock,
    # a month added, read back as UTC. Literals rather than interpolation, as
    # the ETL's SQL is.
    #
    # **A held starter dies with this month**: it is this period's level-1
    # credit, so the level grants count it and do not pay level 1 again.
    op.execute(
        "UPDATE credit_lots SET expires_at = "
        "(date_trunc('month', now() AT TIME ZONE 'UTC') + interval '1 month') AT TIME ZONE 'UTC' "
        "WHERE source = 'profile_completed' AND expires_at IS NULL AND quantity_remaining > 0"
    )
    # **A spent one dies with the month it was granted in**, already past. Given
    # this month's end it would count as this period's level-1 payment — a
    # starter spent in September would leave November owing nothing. It still
    # needs a date, because a refund of it inherits `NULL` as "never expires".
    op.execute(
        "UPDATE credit_lots SET expires_at = "
        "(date_trunc('month', created_at AT TIME ZONE 'UTC') + interval '1 month') "
        "AT TIME ZONE 'UTC' "
        "WHERE source = 'profile_completed' AND expires_at IS NULL"
    )


def downgrade() -> None:
    """Every starter back to never expiring, **which is what the code being
    rolled back to writes**: before decision 308 a starter had no end date, so
    restoring that for all of them — including any the loader wrote after this
    revision with the cutover month's end — is the state that code expects,
    not a loss of anything it reads."""
    op.execute("SET lock_timeout = '3s'")
    op.execute("SET statement_timeout = '30s'")
    op.execute("UPDATE credit_lots SET expires_at = NULL WHERE source = 'profile_completed'")
