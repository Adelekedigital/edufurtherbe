"""``session_type_offerings``: a session type covers up to three offerings.

Settled decision #205, Session Types frontend #9. **The expand step.** The new
table holds the set, in the mentor's order; ``session_types.service_offering_id``
stays and is dual-written as the first of the set, so code from before this
release keeps reading and writing it. Reads fall back to that column for a type
with no rows here, which covers anything the old code writes during the deploy.
Dropping the column is the contract step, in a later release.

Backfilled from the column: every classified type gets its one row. Additive
otherwise; the table is new and nothing old reads it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ceb7410fb394"
down_revision: str | Sequence[str] | None = "6c148bef8b52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "session_type_offerings"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("session_type_id", sa.Uuid(), nullable=False),
        sa.Column("service_offering_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_session_type_offerings")),
        sa.ForeignKeyConstraint(
            ["session_type_id"],
            ["session_types.id"],
            name="fk_session_type_offerings_type",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["service_offering_id"],
            ["service_offerings.id"],
            name="fk_session_type_offerings_offering",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_session_type_offerings_pair",
        TABLE,
        ["session_type_id", "service_offering_id"],
        unique=True,
    )
    op.create_index("ix_session_type_offerings_offering", TABLE, ["service_offering_id"])
    op.execute(
        "CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON session_type_offerings "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    # The backfill: one row, at position 0, for every classified type.
    op.execute(
        "INSERT INTO session_type_offerings (session_type_id, service_offering_id, position) "
        "SELECT id, service_offering_id, 0 FROM session_types "
        "WHERE service_offering_id IS NOT NULL"
    )


def downgrade() -> None:
    """Drop the table. The column still holds each type's first offering, so a
    type loses only its second and third — which the old code cannot express."""
    op.execute("SET lock_timeout = '3s'")
    op.drop_table(TABLE)
