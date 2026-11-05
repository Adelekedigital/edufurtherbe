"""``level_top_up`` joins the credit source vocabulary (decision 307, expand step).

**Merged alone, before the code that writes it** (project-conventions,
*Migration ordering*): the level top-up writes ``credit_lots.source =
'level_top_up'``, and code reaching a database whose ``CHECK`` refuses the value
would fail every onboarding completion until the migration ran. Nothing writes
the value yet, so applying this is inert.

Settled decision #100 is what makes it cheap: the vocabulary is ``text`` plus a
``CHECK``, so adding a value is dropping and recreating one constraint, exactly
as ``c4e8b1a72f95`` did for ``admin_grant``.

Additive. The new CHECK is strictly wider than the one it replaces, so no
existing row can fail it.

**A plain swap, not ``NOT VALID`` then ``VALIDATE``**, the shape
``e5a71c9b382d`` and ``e4b9d2a7c1f3`` chose for small tables: ``credit_lots``
holds a few lots per mentee a month, so the validating scan is milliseconds,
and a constraint left unvalidated looks enforced and is not. ``lock_timeout``
fails fast behind a booking and ``statement_timeout`` bounds the scan, as
``d3a6f81b52c7`` sets both on a constraint change.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3f9c2a7e1b64"
down_revision: str | Sequence[str] | None = "a7d3e5c91b42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_credit_lots_source_is_known"

#: Written out rather than imported: **no migration imports from ``app``**.
#: `test_every_converted_enum_has_a_check_naming_its_values` compares the live
#: constraint against the class, which is what keeps the two honest.
SOURCES_BEFORE = (
    "'profile_completed', 'referral_unlock', 'monthly_free', 'refund', "
    "'opening_balance', 'admin_grant'"
)
SOURCES_AFTER = (
    "'profile_completed', 'referral_unlock', 'monthly_free', 'refund', "
    "'opening_balance', 'admin_grant', 'level_top_up'"
)


def _replace_check(sources: str) -> None:
    op.execute("SET lock_timeout = '3s'")
    op.execute("SET statement_timeout = '30s'")
    op.drop_constraint(op.f(CONSTRAINT), "credit_lots", type_="check")
    op.create_check_constraint(op.f(CONSTRAINT), "credit_lots", f"source IN ({sources})")


def upgrade() -> None:
    """Widen the vocabulary."""
    _replace_check(SOURCES_AFTER)


def downgrade() -> None:
    """Narrow it back. **Refuses while any ``level_top_up`` lot exists**, and
    says so: those lots are somebody's credits, and rewriting their source to
    let a downgrade pass is the quiet data change the ledger exists to prevent.
    Checked first, so the operator reads the reason rather than a bare
    constraint violation."""
    if (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS (SELECT 1 FROM credit_lots WHERE source = 'level_top_up')"))
        .scalar()
    ):
        raise RuntimeError(
            "credit_lots holds level_top_up lots: they are mentees' credits, so "
            "downgrading would have to rewrite them; move them deliberately first"
        )
    _replace_check(SOURCES_BEFORE)
