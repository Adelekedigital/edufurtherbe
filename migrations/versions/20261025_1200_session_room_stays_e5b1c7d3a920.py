"""``session_room_stays``: who is in a session's Daily room now (#394).

Session Join PRD, founder decisions 2026-10-21. ``in_room_at`` on
``session_participants`` says a party *has been* in the room; the join page
needs *is in it now*. One row per Daily join: Daily names each join with its
own ``session_id`` (``provider_session_id`` here), sent on both
``participant.joined`` and ``participant.left``. A party is in the room while
one of their stays has no ``left_at``.

``id`` is the generated key (ADR 0015) and ``(participant_id,
provider_session_id)`` is ``UNIQUE``, which is what lets a repeated or
out-of-order delivery change nothing. Rows cascade with their participant, as
the participant does with its session.

Expand only, and merged alone (project-conventions, *Migration ordering*):
nothing reads or writes the table until the next PR. No backfill: presence is
live state, and a call already running when the code deploys is in it once its
next join or leave arrives.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5b1c7d3a920"
down_revision: str | Sequence[str] | None = "b7779986392d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "session_room_stays"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("provider_session_id", sa.Text(), nullable=False),
        sa.Column("joined_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("left_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
            "char_length(provider_session_id) BETWEEN 1 AND 128",
            name=op.f("ck_session_room_stays_provider_session_id_length"),
        ),
        sa.CheckConstraint(
            "left_at IS NULL OR left_at >= joined_at",
            name=op.f("ck_session_room_stays_left_after_joined"),
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"],
            ["session_participants.id"],
            name=op.f("fk_session_room_stays_participant_id_session_participants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_session_room_stays")),
    )
    op.create_index(
        "uq_session_room_stays_join",
        TABLE,
        ["participant_id", "provider_session_id"],
        unique=True,
    )
    op.execute(
        f"CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON {TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    """Drop the table. Live state only: losing it reads everybody as not in the
    room until their next join, and attendance is untouched."""
    op.execute("SET lock_timeout = '3s'")
    op.drop_table(TABLE)
