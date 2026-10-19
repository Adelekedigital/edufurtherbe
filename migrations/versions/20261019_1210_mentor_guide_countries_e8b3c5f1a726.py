"""The countries a mentor can guide mentees towards: ``mentor_guide_countries``.

Mentor Settings PRD, owner 2026-10-19 (decision 276). A mentor names up to
eight, in their own order, written as one list on the mentor-profile PATCH.

**The eight is structural.** ``position`` is unique per mentor and below 8, so a
ninth row has no place to go and there is no count to race. ``mentor_user_id``
references ``mentor_profiles(user_id)``, like ``mentor_service_offerings``, so a
row cannot belong to a non-mentor; it cascades with the profile and restricts on
the country. No backfill: nothing held this before.

Expand only, and merged alone (project-conventions, *Migration ordering*):
nothing reads or writes the table until the next PR, so this lands inert.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e8b3c5f1a726"
down_revision: str | Sequence[str] | None = "d6f2a9c4e185"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "mentor_guide_countries"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("mentor_user_id", sa.Uuid(), nullable=False),
        sa.Column("country_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
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
        sa.CheckConstraint(
            "position >= 0 AND position < 8",
            name=op.f("ck_mentor_guide_countries_position_in_range"),
        ),
        sa.ForeignKeyConstraint(
            ["mentor_user_id"],
            ["mentor_profiles.user_id"],
            name="fk_mentor_guide_countries_mentor",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["country_id"],
            ["countries.id"],
            name="fk_mentor_guide_countries_country",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mentor_guide_countries")),
    )
    op.create_index(
        "ix_mentor_guide_countries_pair", TABLE, ["mentor_user_id", "country_id"], unique=True
    )
    op.create_index(
        "ix_mentor_guide_countries_position", TABLE, ["mentor_user_id", "position"], unique=True
    )
    op.create_index("ix_mentor_guide_countries_country", TABLE, ["country_id"])
    op.execute(
        f"CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON {TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    """**Refuse** while a mentor has named a country: dropping the table would
    delete what they chose, and that is an operator's call."""
    op.execute("SET lock_timeout = '3s'")
    held = (
        op.get_bind().execute(sa.text("SELECT count(*) FROM mentor_guide_countries")).scalar_one()
    )
    if held:
        raise RuntimeError(f"{held} guide-to country row(s) would be deleted by the downgrade")
    op.drop_table(TABLE)
