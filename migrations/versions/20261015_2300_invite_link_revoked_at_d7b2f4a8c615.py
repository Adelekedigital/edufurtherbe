"""A personal invite link can be revoked: ``invite_links.revoked_at``.

Owner, 2026-10-15. The owner may switch their link off and on again. While
revoked, its code resolves for nobody, claim and landing lookup alike, yet stays
the owner's: it is still the current code on their row, so `UNIQUE (code)` keeps
anyone else from picking it, and reactivating makes the same code work again.

**Nullable, no default, no backfill: null means active**, which is every
existing row. A timestamp rather than a boolean, for when it was switched off,
the shape `referrals` was always going to use for a revocation.

Expand only, and merged alone (project-conventions, *Migration ordering*):
nothing reads the column until the next PR. Adding a nullable column with no
default is a catalogue change; it takes a brief `ACCESS EXCLUSIVE` lock on a
small table and rewrites nothing.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d7b2f4a8c615"
down_revision: str | Sequence[str] | None = "c5a1e7f3d920"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.add_column("invite_links", sa.Column("revoked_at", sa.TIMESTAMP(timezone=True)))


def downgrade() -> None:
    """Drop the column.

    What it held is lost: which links are revoked. Every link becomes active
    again, so a revoked code resolves after a downgrade. Nothing reads the
    column until the code that writes it ships.
    """
    op.execute("SET lock_timeout = '3s'")
    op.drop_column("invite_links", "revoked_at")
