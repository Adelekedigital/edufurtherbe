"""``mentee_goal_schools``: the schools a mentee's goal names, in order.

Mentee Settings PRD. **Migration-first** (project-conventions, *Migration
ordering*): nothing reads or writes the table in this release; the endpoint
lands in the next PR, after this is applied.

Keyed on ``mentee_goals.id`` with ``ON DELETE CASCADE``, so a cleared goal takes
its schools. The education entry's pair: ``school_name_raw`` always kept,
``institution_id`` nullable and ``RESTRICT`` like ``education_entries``.
``UNIQUE (goal_id, position)`` keeps the mentee's order total, and
``position BETWEEN 0 AND 9`` makes the ten-school cap the database's too.

Additive; the downgrade drops the table and the schools in it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2f8b6a4c1e7"
down_revision: str | Sequence[str] | None = "e4b9d2a7c1f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "mentee_goal_schools"
#: Copied, not imported: a migration never imports a live symbol (decision 105).
#: `test_the_bounds_are_the_domains` pins both to `app.domain.goals`.
SCHOOL_NAME_MAX_LENGTH = 120
MAX_GOAL_SCHOOLS = 10


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("goal_id", sa.Uuid(), nullable=False),
        sa.Column("institution_id", sa.Uuid(), nullable=True),
        sa.Column("school_name_raw", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mentee_goal_schools")),
        sa.ForeignKeyConstraint(
            ["goal_id"],
            ["mentee_goals.id"],
            name="fk_mentee_goal_schools_goal",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["institution_id"],
            ["institutions.id"],
            name="fk_mentee_goal_schools_institution",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("goal_id", "position", name="uq_mentee_goal_schools_goal_position"),
        sa.CheckConstraint(
            f"char_length(school_name_raw) BETWEEN 1 AND {SCHOOL_NAME_MAX_LENGTH}",
            name=op.f("ck_mentee_goal_schools_school_name_raw_length"),
        ),
        sa.CheckConstraint(
            f"position BETWEEN 0 AND {MAX_GOAL_SCHOOLS - 1}",
            name=op.f("ck_mentee_goal_schools_position_within_cap"),
        ),
    )
    op.create_index("ix_mentee_goal_schools_institution", TABLE, ["institution_id"])
    op.execute(
        "CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON mentee_goal_schools "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.drop_table(TABLE)
