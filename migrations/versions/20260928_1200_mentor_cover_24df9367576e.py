"""``user_profiles.cover_color`` and ``cover_art``: a mentor's cover without a banner.

Settled decision #193, frontend request #19. ``cover_color`` is one of the
design's twelve keys or NULL for the automatic colour the client derives from
the id; ``cover_art`` is ``none``, ``icons``, ``pattern`` or ``single``.
Text + CHECK (#100), the values rendered from the enums at the time of writing.

Additive: a nullable column and a defaulted one, metadata-only on PostgreSQL
11+; every existing row reads automatic colour and no art, which both CHECKs
allow, so both code versions serve during the deploy.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "24df9367576e"
down_revision: str | Sequence[str] | None = "a41f0c7e92b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "user_profiles"
COLOURS = (
    "sky", "ice", "aqua", "mint", "sage", "lemon",
    "sand", "peach", "blush", "rose", "lilac", "mist",
)  # fmt: skip
ARTS = ("none", "icons", "pattern", "single")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.add_column(TABLE, sa.Column("cover_color", sa.Text(), nullable=True))
    op.add_column(
        TABLE,
        sa.Column("cover_art", sa.Text(), server_default=sa.text("'none'"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_user_profiles_cover_color_is_known"), TABLE, _in("cover_color", COLOURS)
    )
    op.create_check_constraint(
        op.f("ck_user_profiles_cover_art_is_known"), TABLE, _in("cover_art", ARTS)
    )


def downgrade() -> None:
    """Drop both columns; covers go back to the automatic colour and no art."""
    op.execute("SET lock_timeout = '3s'")
    op.drop_constraint(op.f("ck_user_profiles_cover_art_is_known"), TABLE, type_="check")
    op.drop_constraint(op.f("ck_user_profiles_cover_color_is_known"), TABLE, type_="check")
    op.drop_column(TABLE, "cover_art")
    op.drop_column(TABLE, "cover_color")
