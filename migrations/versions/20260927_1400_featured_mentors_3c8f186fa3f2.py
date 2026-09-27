"""``featured_mentors`` — who was "Featured this week", and in which rotation.

The first request of a week picks a mentor and writes a row; the rest of the
week reads it. `cycle` numbers the rotations, so nobody is featured twice until
every bookable mentor has had a turn (settled decision #177).

Unique on ``(week_start, mentor_user_id)``: a week holds a second row only when
its mentor stopped being bookable and was replaced, never the same mentor twice.

Additive and new; nothing existing reads it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3c8f186fa3f2"
down_revision: str | Sequence[str] | None = "5930c3cc1109"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "featured_mentors"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("mentor_user_id", sa.Uuid(), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("cycle", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_featured_mentors")),
        sa.ForeignKeyConstraint(
            ["mentor_user_id"],
            ["mentor_profiles.user_id"],
            name=op.f("fk_featured_mentors_mentor_user_id_mentor_profiles"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("week_start", "mentor_user_id", name="uq_featured_mentors_week_mentor"),
    )
    # "Who has had a turn this cycle" is asked on every new pick.
    op.create_index("ix_featured_mentors_cycle", TABLE, ["cycle"])


def downgrade() -> None:
    """Drop the table. The rotation history goes with it; the next pick starts cycle 1."""
    op.execute("SET lock_timeout = '3s'")
    op.drop_index("ix_featured_mentors_cycle", table_name=TABLE)
    op.drop_table(TABLE)
