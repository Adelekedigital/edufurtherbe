"""Mark a placeholder profile handle (owner, 2026-11-14).

``users.slug_is_placeholder`` (boolean, NOT NULL, default false). An account
is created at first sign-in, before it has a name, with a ``user-…`` handle;
this flag says that handle may be replaced, once, by one made from the name.
Text alone cannot say it: an imported handle could start with ``user-``.

Expand only: a constant default makes this a catalogue change on Postgres 11+,
so no table rewrite and no long lock. Every existing row reads false, which is
right: every handle that exists today was imported and is final.

Downgrade drops the column; handles themselves are kept.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d81f2a6e57"
down_revision: str | Sequence[str] | None = "b7c2e4a91f30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.add_column(
        "users",
        sa.Column("slug_is_placeholder", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.drop_column("users", "slug_is_placeholder")
