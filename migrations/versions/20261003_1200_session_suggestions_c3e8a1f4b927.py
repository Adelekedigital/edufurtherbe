"""``session_suggestions`` — a time a mentor suggests when declining or cancelling.

Owner decisions of 2026-10-03 (#339, settled decision 230): one suggested time,
held for its mentee for two hours. The table is new and nothing reads it before
this release, so both code versions serve during the deploy.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3e8a1f4b927"
down_revision: str | Sequence[str] | None = "c3f8a51d7e20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "session_suggestions"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("mentor_id", sa.Uuid(), nullable=False),
        sa.Column("mentee_id", sa.Uuid(), nullable=False),
        sa.Column("session_type_id", sa.Uuid(), nullable=False),
        sa.Column("starts_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("held_until", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("accepted_session_id", sa.Uuid(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_session_suggestions")),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name=op.f("fk_session_suggestions_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["mentor_id"],
            ["users.id"],
            name=op.f("fk_session_suggestions_mentor_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["mentee_id"],
            ["users.id"],
            name=op.f("fk_session_suggestions_mentee_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["session_type_id"],
            ["session_types.id"],
            name=op.f("fk_session_suggestions_session_type_id_session_types"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["accepted_session_id"],
            ["sessions.id"],
            name=op.f("fk_session_suggestions_accepted_session_id_sessions"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "duration_minutes > 0", name=op.f("ck_session_suggestions_duration_is_positive")
        ),
        sa.CheckConstraint(
            "mentor_id <> mentee_id", name=op.f("ck_session_suggestions_parties_differ")
        ),
    )
    op.create_index("uq_session_suggestions_session", TABLE, ["session_id"], unique=True)
    op.create_index(
        "ix_session_suggestions_open_holds",
        TABLE,
        ["mentor_id", "held_until"],
        postgresql_where=sa.text("accepted_session_id IS NULL"),
    )
    op.execute(
        f"CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON {TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    """Drop the table. A suggestion is an offer, not a record of anything that
    happened — the decline or cancel it came with is in ``session_events`` — so
    losing the rows loses only offers, and any it produced are ordinary sessions
    that keep existing."""
    op.execute("SET lock_timeout = '3s'")
    op.drop_index("ix_session_suggestions_open_holds", table_name=TABLE)
    op.drop_index("uq_session_suggestions_session", table_name=TABLE)
    op.drop_table(TABLE)
