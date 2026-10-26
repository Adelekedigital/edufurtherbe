"""One invite bonus per user, ever (decision 295).

**The bonus lost its database gate when the unlock started waiting.** Until
decision 295 the 2-credit bonus was paid in the same statement that inserted
the referrer's `referral_unlocks` row, so `uq_referral_unlocks_user_id` made it
once-ever. The unlock now waits for an attended session, and the bonus is paid
at the first qualified invite under the referrer's row lock instead. This index
is the backstop for any future writer that skips that lock: a second
`referral_unlock` lot for a user is refused rather than paid.

**Partial on the source**, like the starter's and the opening balance's: on
`user_id` alone a user could hold one lot of any kind.

**Additive, and existing rows cannot violate it**: the old gate paid at most
one `referral_unlock` lot per user, and nothing else writes that source.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8489be263031"
down_revision: str | Sequence[str] | None = "7c41e9b2a6d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "uq_credit_lots_one_referral_bonus_per_user"


def upgrade() -> None:
    """Build the index without blocking writes to `credit_lots`.

    **`CONCURRENTLY`, like `a1c7e46b02d9`**, and unlike the ledger's first
    indexes, which were built on tables created empty in the same migration.
    `credit_lots` holds rows and is written by every booking, refund and grant,
    so an inline build would take `SHARE` and stall them, and every statement
    queued behind it. `lock_timeout` fails fast instead of joining that queue;
    no `statement_timeout`, which would leave an `INVALID` index half-built.

    **A retry rebuilds a failed build rather than skipping it.** A concurrent
    build that dies (the lock timeout, an interrupted deploy) leaves an
    `INVALID` index of this name, and `IF NOT EXISTS` alone would then report
    success with nothing enforced. So an invalid leftover is dropped first.
    Checked from here because `DROP INDEX CONCURRENTLY` cannot run in a `DO`
    block.
    """
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '3s'")
        invalid = op.get_bind().scalar(
            sa.text(
                "SELECT NOT i.indisvalid FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = :name"
            ),
            {"name": INDEX},
        )
        if invalid:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
        op.execute(
            f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} ON credit_lots "
            "(user_id) WHERE source = 'referral_unlock'"
        )


def downgrade() -> None:
    """Drop it, also concurrently. `IF EXISTS` because a failed concurrent
    build leaves an `INVALID` index of this name behind, and a downgrade that
    could not remove it would strand the chain."""
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '3s'")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
