"""``mentor_next_availability``, and the trigger that marks it stale.

One stored derived value, allowed by ADR 0029 and for the discovery card only:
when each bookable mentor is next free. A QStash job computes it; nothing that
*decides* anything reads it. Booking still reads live slots.

**The trigger is the part that matters.** Eight tables decide when a mentor is
free, and a card that kept showing a time after one of them changed would show
a time already taken. Each carries ``trg_mark_next_available_stale``, which sets
the mentor's ``changed_at``; the card shows the value only while the refresh
that wrote it started after that. A trigger rather than application hooks
because the write paths into those tables are many and a hook missed in one of
them is silent — `test_every_availability_table_marks_the_mentor_stale` pins the
list against ``pg_trigger``.

The function reads the mentor's id from a named column of ``NEW``/``OLD`` (both,
so a row moved between mentors marks both), and for the two session-type
children looks the mentor up through ``session_types``. It only ever
``UPDATE``s: a mentor with no row yet has nothing to mark, and the job creates
the row stale.

Additive and new. No existing code reads the table, so old and new pods agree
during the deploy; the triggers write only to the new table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5930c3cc1109"
down_revision: str | Sequence[str] | None = "c4e8b1a72f95"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "mentor_next_availability"
TRIGGER = "trg_mark_next_available_stale"
FUNCTION = "mark_next_available_stale"

#: Each table that decides when a mentor is free, the column naming the mentor,
#: and whether that column is a session type rather than the mentor.
#: Written out rather than imported: no migration imports from ``app``.
WATCHED: tuple[tuple[str, str, bool], ...] = (
    ("sessions", "mentor_id", False),
    ("availability_rules", "mentor_user_id", False),
    ("availability_exceptions", "mentor_user_id", False),
    ("session_types", "mentor_user_id", False),
    ("session_type_booking_configs", "session_type_id", True),
    ("session_type_scheduling_windows", "session_type_id", True),
    ("mentor_profiles", "user_id", False),
    ("calendar_connections", "user_id", False),
)


def upgrade() -> None:
    """The table, its ``updated_at`` trigger, the stale function, eight triggers."""
    op.execute("SET lock_timeout = '3s'")

    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("mentor_user_id", sa.Uuid(), nullable=False),
        sa.Column("next_available_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("computed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "changed_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mentor_next_availability")),
        sa.ForeignKeyConstraint(
            ["mentor_user_id"],
            ["mentor_profiles.user_id"],
            name=op.f("fk_mentor_next_availability_mentor_user_id_mentor_profiles"),
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "uq_mentor_next_availability_mentor_user_id",
        TABLE,
        ["mentor_user_id"],
        unique=True,
    )
    op.execute(
        f"CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON {TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    # `clock_timestamp()`, not `now()`: `now()` is the transaction's start, so a
    # booking in a transaction that began before a refresh started would be
    # marked earlier than the refresh and read as already accounted for.
    op.execute(
        """
        CREATE FUNCTION mark_next_available_stale() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            ids uuid[] := '{}';
            value text;
        BEGIN
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                value := to_jsonb(NEW) ->> TG_ARGV[0];
                IF value IS NOT NULL THEN ids := ids || value::uuid; END IF;
            END IF;
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                value := to_jsonb(OLD) ->> TG_ARGV[0];
                IF value IS NOT NULL THEN ids := ids || value::uuid; END IF;
            END IF;
            IF coalesce(TG_ARGV[1], '') = 'session_type' THEN
                SELECT coalesce(array_agg(mentor_user_id), '{}') INTO ids
                FROM session_types WHERE id = ANY(ids);
            END IF;
            UPDATE mentor_next_availability SET changed_at = clock_timestamp()
            WHERE mentor_user_id = ANY(ids);
            RETURN NULL;
        END
        $$
        """
    )
    for table, column, via_session_type in WATCHED:
        args = f"'{column}', 'session_type'" if via_session_type else f"'{column}'"
        op.execute(
            f"CREATE TRIGGER {TRIGGER} AFTER INSERT OR UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {FUNCTION}({args})"
        )


def downgrade() -> None:
    """Drop the triggers, the function, then the table.

    Nothing is lost that matters: every row is recomputable from the tables the
    triggers watch, which is what makes this a cache.
    """
    op.execute("SET lock_timeout = '3s'")
    for table, _column, _via in WATCHED:
        op.execute(f"DROP TRIGGER IF EXISTS {TRIGGER} ON {table}")
    op.execute(f"DROP FUNCTION IF EXISTS {FUNCTION}()")
    op.drop_index("uq_mentor_next_availability_mentor_user_id", table_name=TABLE)
    op.drop_table(TABLE)
