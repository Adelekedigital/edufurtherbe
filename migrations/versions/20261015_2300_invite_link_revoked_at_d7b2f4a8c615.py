"""A personal invite link can be revoked: ``invite_links.revoked_at``.

Owner, 2026-10-15. The owner may switch their link off and on again. While
revoked, its code resolves for nobody, claim and landing lookup alike, yet stays
the owner's: it is still the current code on their row, so `UNIQUE (code)` keeps
anyone else from picking it, and reactivating makes the same code work again.

**Nullable, no default, no backfill: null means active**, which is every
existing row. A CHECK keeps a revocation after the link's creation. A
timestamp rather than a boolean, for when it was switched off, the shape
`referrals` was always going to use for a revocation.

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
    op.create_check_constraint(
        op.f("ck_invite_links_revoked_after_created"),
        "invite_links",
        "revoked_at IS NULL OR revoked_at >= created_at",
    )


def downgrade() -> None:
    """Drop the column, **refusing while any link is revoked**.

    Dropping it would switch every revoked link back on: a code its owner
    turned off, perhaps because it leaked, would attribute arrivals again with
    nobody choosing that. So a downgrade over revoked links stops and says so;
    reactivating or deleting them first is a decision for a person.
    """
    op.execute("SET lock_timeout = '3s'")
    revoked = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM invite_links WHERE revoked_at IS NOT NULL"))
        .scalar_one()
    )
    if revoked:
        raise RuntimeError(
            f"{revoked} invite link(s) are revoked; dropping revoked_at would reactivate them"
        )
    op.drop_constraint(op.f("ck_invite_links_revoked_after_created"), "invite_links")
    op.drop_column("invite_links", "revoked_at")
