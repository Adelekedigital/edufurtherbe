"""``session_type_stages``: a session type is aimed at several application stages.

Settled decision #215, Session Types frontend round 3 A. **The expand step**, the
#205 shape: the new table holds the set in the mentor's order, and
``session_types.application_stage`` stays, dual-written as the first of the set,
so code from before this release keeps reading and writing it. Reads fall back
to that column for a type with no rows here, which covers anything the old code
writes during the deploy. Dropping the column is the contract step.

**The label constraint loosens to one direction.** It was symmetric —
``(application_stage = 'other') = (custom_stage_label IS NOT NULL)`` — which
refuses ``[drafting_stage, other]`` with a label, a legal set whose first is not
``other``. It becomes *``other`` first implies a label*; the other direction
needs the set, which a ``CHECK`` cannot see, and moves to the application. Old
code writes only single stages, which the looser form accepts.

Backfilled from the column: every staged type gets its one row.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e0d7461f13ce"
down_revision: str | Sequence[str] | None = "7b3e91c4a2d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "session_type_stages"
LABEL_CHECK = "ck_session_types_custom_stage_label_matches_stage"
SYMMETRIC = "(application_stage = 'other') = (custom_stage_label IS NOT NULL)"
ONE_WAY = "application_stage IS DISTINCT FROM 'other' OR custom_stage_label IS NOT NULL"
#: `ApplicationStage`, written out: no migration imports from `app`.
STAGES = (
    "early_exploration",
    "drafting_stage",
    "post_submission",
    "revisions",
    "interviewing",
    "other",
)


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("session_type_id", sa.Uuid(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_session_type_stages")),
        sa.ForeignKeyConstraint(
            ["session_type_id"],
            ["session_types.id"],
            name="fk_session_type_stages_type",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "stage IN (" + ", ".join(f"'{s}'" for s in STAGES) + ")",
            name=op.f("ck_session_type_stages_stage_is_known"),
        ),
    )
    op.create_index("ix_session_type_stages_pair", TABLE, ["session_type_id", "stage"], unique=True)
    # One stage per place in the order, so the order is total.
    op.create_index(
        "ix_session_type_stages_position", TABLE, ["session_type_id", "position"], unique=True
    )
    op.execute(
        "CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON session_type_stages "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    # The backfill: one row, at position 0, for every staged type.
    op.execute(
        "INSERT INTO session_type_stages (session_type_id, stage, position) "
        "SELECT id, application_stage, 0 FROM session_types "
        "WHERE application_stage IS NOT NULL"
    )
    op.drop_constraint(op.f(LABEL_CHECK), "session_types", type_="check")
    op.create_check_constraint(op.f(LABEL_CHECK), "session_types", ONE_WAY)


def downgrade() -> None:
    """Drop the table and restore the symmetric constraint.

    A type whose set held `other` behind another stage keeps its label, so its
    single stage becomes `other` — the one stage the label belongs to — rather
    than losing the mentor's wording. It loses its other stages, which the old
    code cannot express.
    """
    op.execute("SET lock_timeout = '3s'")
    op.execute(
        "UPDATE session_types SET application_stage = 'other' "
        "WHERE custom_stage_label IS NOT NULL "
        "AND application_stage IS DISTINCT FROM 'other'"
    )
    op.drop_constraint(op.f(LABEL_CHECK), "session_types", type_="check")
    op.create_check_constraint(op.f(LABEL_CHECK), "session_types", SYMMETRIC)
    op.drop_table(TABLE)
