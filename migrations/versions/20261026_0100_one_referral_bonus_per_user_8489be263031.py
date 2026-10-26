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

LOTS = "credit_lots"
INDEX = "uq_credit_lots_one_referral_bonus_per_user"


def upgrade() -> None:
    op.create_index(
        INDEX,
        LOTS,
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("source = 'referral_unlock'"),
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name=LOTS)
