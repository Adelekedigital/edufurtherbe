"""``mentor_next_availability``, and the trigger that marks it stale.

One stored derived value, allowed by ADR 0029 and for the discovery card only:
when each bookable mentor is next free. A QStash job computes it; nothing that
*decides* anything reads it. Booking still reads live slots.

**The trigger is the part that matters.** Eight tables decide when a mentor is
free, and a card that kept showing a time after one of them changed would show
a time already taken. Each carries ``trg_mark_next_available_stale``, which sets
the mentor's ``changed_at``. A refresh records the ``changed_at`` it **saw** in
``seen_changed_at``, and the card shows the value only while the two are equal
— an equality on one database value, so no clock is compared with another and
a change whose transaction was still open when the refresh read the row breaks
it on commit. A trigger rather than application hooks because the write paths
into those tables are many and a hook missed in one of them is silent;
`test_every_availability_table_marks_the_mentor_stale` pins the list against
``pg_trigger``.

The function reads the mentor's id from a named column of ``NEW``/``OLD`` (both,
so a row moved between mentors marks both), and for the two session-type
children looks the mentor up through ``session_types``. It **upserts**: a
mentor with no row yet gets one, because a change made before the job first
reaches a mentor must still be seen. Only ids that are mentors get a row, so a
non-mentor's calendar connection never trips the foreign key.

An ``UPDATE`` that changes nothing but ``updated_at`` marks nothing, and the two
tables with frequent unrelated writes — ``calendar_connections``, whose health
check stamps ``last_synced_at`` hourly, and ``mentor_profiles``, whose bio and
headline change often — fire only on the columns that decide availability.

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

#: Each table that decides when a mentor is free: the column naming the mentor,
#: whether that column is a session type rather than the mentor, and the
#: columns an ``UPDATE`` must touch to matter (``None``: any column).
#: Written out rather than imported: no migration imports from ``app``.
WATCHED: tuple[tuple[str, str, bool, str | None], ...] = (
    ("sessions", "mentor_id", False, None),
    ("availability_rules", "mentor_user_id", False, None),
    ("availability_exceptions", "mentor_user_id", False, None),
    ("session_types", "mentor_user_id", False, None),
    ("session_type_booking_configs", "session_type_id", True, None),
    ("session_type_scheduling_windows", "session_type_id", True, None),
    # What `mentor_is_public()` reads. Bio, headline and the rest do not move a
    # slot, and marking on them would blank a card for every profile edit.
    ("mentor_profiles", "user_id", False, "user_id, approval_status, listing_status, deleted_at"),
    # Whether the grant is live. `last_synced_at` and `last_error` are stamped
    # by the hourly health check and would blank every connected mentor's card.
    ("calendar_connections", "user_id", False, "user_id, status"),
)

STALE_FUNCTION = """
CREATE FUNCTION mark_next_available_stale() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    ids uuid[] := '{}';
    value text;
BEGIN
    -- `set_updated_at` has already moved `updated_at`, so an UPDATE that
    -- changed nothing else still differs as a record; compare without it.
    IF TG_OP = 'UPDATE' AND (to_jsonb(OLD) - 'updated_at') = (to_jsonb(NEW) - 'updated_at') THEN
        RETURN NULL;
    END IF;
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
    -- `clock_timestamp()`, not `now()`: only so successive marks differ; the
    -- card compares this value for equality, never against another clock.
    INSERT INTO mentor_next_availability (mentor_user_id, changed_at)
    SELECT user_id, clock_timestamp() FROM mentor_profiles WHERE user_id = ANY(ids)
    ON CONFLICT (mentor_user_id) DO UPDATE SET changed_at = excluded.changed_at;
    RETURN NULL;
END
$$
"""


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
        sa.Column("seen_changed_at", sa.TIMESTAMP(timezone=True), nullable=True),
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

    op.execute(STALE_FUNCTION)
    for table, column, via_session_type, columns in WATCHED:
        args = f"'{column}', 'session_type'" if via_session_type else f"'{column}'"
        on_update = f"UPDATE OF {columns}" if columns else "UPDATE"
        op.execute(
            f"CREATE TRIGGER {TRIGGER} AFTER INSERT OR {on_update} OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {FUNCTION}({args})"
        )


def downgrade() -> None:
    """Drop the triggers, the function, then the table.

    Nothing is lost that matters: every row is recomputable from the tables the
    triggers watch, which is what makes this a cache.
    """
    op.execute("SET lock_timeout = '3s'")
    for table, _column, _via, _columns in WATCHED:
        op.execute(f"DROP TRIGGER IF EXISTS {TRIGGER} ON {table}")
    op.execute(f"DROP FUNCTION IF EXISTS {FUNCTION}()")
    op.drop_index("uq_mentor_next_availability_mentor_user_id", table_name=TABLE)
    op.drop_table(TABLE)
