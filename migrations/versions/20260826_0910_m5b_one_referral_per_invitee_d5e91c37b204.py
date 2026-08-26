"""An invitee belongs to at most one referral.

**The defect this closes locked users out permanently.** Nothing stopped a
person claiming two invite codes: both claims answered 200, and their next
``POST /me/onboarding/completion`` ran ``qualify_invitee``, whose
``.one_or_none()`` then raised ``MultipleResultsFound``. That is not an
``AppError``, so it fell through to a 500 and rolled the whole transaction
back — **every retry hit the same 500**. The user could never finish
onboarding, never received their starter credit, and had no way to un-claim.

Reproduced end to end before this was written: two codes, ``CLAIM1=200``,
``CLAIM2=200``, completion ``MultipleResultsFound``.

**Structural rather than an application check**, for the reason every other
once-only rule in this phase is: the guard has to hold against a producer
somebody later makes idempotent by retrying, and a read-then-write loses the
race between two claims arriving together. ``claim_referral`` refuses the second
claim with a 409 so the caller learns *why*; this is what makes it true.

Replaces the non-unique ``ix_referrals_invitee``. That index was created for
attribution lookups and still serves them — a unique index answers the same
query — so this is a tightening rather than an addition.

Partial on ``invitee_user_id IS NOT NULL``, because an unclaimed invite has no
invitee and any number of those coexist. PostgreSQL treats nulls as distinct so
the predicate is not strictly required, but it is written anyway: it states the
rule the index is *for*, and it keeps the index off the many rows nobody ever
arrived through.

Additive against existing data: no invitee has claimed twice, because the
endpoint that would allow it ships in this same PR.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e91c37b204"
# Chains off PR 2, which created the index this replaces. PR 6's migration
# re-points at this one — Alembic's chain is linear and a stacked migration
# fixes its `down_revision` in merge order (how-we-work rule 1).
down_revision: str | Sequence[str] | None = "a4f28c15b9e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REFERRALS = "referrals"


def upgrade() -> None:
    """Swap the attribution index for a unique one."""
    op.drop_index("ix_referrals_invitee", table_name=REFERRALS)
    op.create_index(
        "uq_referrals_invitee",
        REFERRALS,
        ["invitee_user_id"],
        unique=True,
        postgresql_where=sa.text("invitee_user_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_referrals_invitee", table_name=REFERRALS)
    op.create_index(
        "ix_referrals_invitee",
        REFERRALS,
        ["invitee_user_id"],
        postgresql_where=sa.text("invitee_user_id IS NOT NULL"),
    )
