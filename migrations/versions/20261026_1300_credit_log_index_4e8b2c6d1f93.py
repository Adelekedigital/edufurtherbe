"""An index for the admin credit log: the whole ledger, newest first.

Admin PRD, 2026-10-26: `GET /admin/credit-log` pages every user's
`credit_transactions` newest first on a `(created_at, id)` keyset. The only
existing ordering index leads on `user_id`, so the log scanned and sorted the
whole table. Measured at 48,000 ledger rows: 183 ms for a first page, a
sequential scan and a sort.

**Purely additive, and invisible to application code.** An index changes no
result, so old and new code side by side cannot disagree. No migrate or
contract phase. Ships alone, before the code that reads it (*Migration
ordering*).

Not partial: every `type` filter of the log reads this order, and the filters
are residual predicates over it.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "4e8b2c6d1f93"
down_revision: str | Sequence[str] | None = "9d3b7f1a2c64"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_credit_transactions_created"


def upgrade() -> None:
    """Built `CONCURRENTLY`, as `a1c7e46b02d9` explains: the ledger holds rows
    and live code writes it, so an inline build would block every write.
    `lock_timeout` fails fast; no `statement_timeout`, which would leave an
    `INVALID` index half built."""
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '3s'")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} "
            "ON credit_transactions (created_at DESC, id DESC)"
        )


def downgrade() -> None:
    """Drop it, also concurrently. Loses nothing; `IF EXISTS` so a failed build's
    `INVALID` index cannot strand the chain."""
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '3s'")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
