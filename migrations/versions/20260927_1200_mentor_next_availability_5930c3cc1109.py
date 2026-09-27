"""``mentor_next_availability``, and the change log that tells the card to stop vouching.

One stored derived value, allowed by ADR 0029 and for the discovery card only:
when each bookable mentor is next free. A QStash job computes it; nothing that
*decides* anything reads it. Booking still reads live slots.

**The card must never show a time already taken**, so every change to anything
that decides a mentor's availability has to be seen. Nine tables decide it, and
each carries ``trg_log_availability_change``, which **appends** a row to
``mentor_availability_changes``. The job snapshots a mentor's change rows,
computes, then writes its answer and deletes exactly the rows it snapshotted.
The card shows the answer only while the mentor has no change rows left. A
change committed during the compute, or one whose transaction was still open
when the snapshot was taken, is a row the job never saw and never deletes.

**Append, not update, because of locks.** An earlier version bumped a
``changed_at`` on the mentor's one cache row. Every booking then locked that row
for the whole booking transaction — including the call that provisions a
meeting — so two mentees booking the same mentor at different times queued
behind each other. Inserts into a log do not block one another.

A trigger rather than application hooks, because the write paths into these
tables are many and a hook missed in one of them is silent;
`test_every_availability_table_logs_a_change` pins the list against
``pg_trigger``. Only ids that are mentors are logged, so a non-mentor's calendar
connection never trips the foreign key.

**What does not log.** An ``UPDATE`` that changes nothing but ``updated_at``.
On ``mentor_profiles``, ``users`` and ``calendar_connections``, columns other
than those deciding availability — bio edits and hourly health-check stamps
would otherwise blank cards constantly; a test pins these column lists to what
``mentor_is_public()`` reads. On ``sessions``, a session that had already ended
before and after the write: settling yesterday's session frees nothing ahead.

Additive and new. No existing code reads either table, so old and new pods
agree during the deploy; the triggers write only to the new log.
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
LOG = "mentor_availability_changes"
TRIGGER = "trg_log_availability_change"
FUNCTION = "log_availability_change"

#: Each table that decides when a mentor is free: the column naming the mentor,
#: how to read it (``mentor``, ``session_type`` to look the mentor up, or
#: ``session`` to also skip sessions that had already ended), and the columns an
#: ``UPDATE`` must touch to matter (``None``: any column).
#: Written out rather than imported: no migration imports from ``app``.
WATCHED: tuple[tuple[str, str, str, str | None], ...] = (
    ("sessions", "mentor_id", "session", "mentor_id, starts_at, duration_minutes, status"),
    ("availability_rules", "mentor_user_id", "mentor", None),
    ("availability_exceptions", "mentor_user_id", "mentor", None),
    ("session_types", "mentor_user_id", "mentor", None),
    ("session_type_booking_configs", "session_type_id", "session_type", None),
    ("session_type_scheduling_windows", "session_type_id", "session_type", None),
    (
        "mentor_profiles",
        "user_id",
        "mentor",
        "user_id, approval_status, listing_status, deleted_at",
    ),
    ("users", "id", "mentor", "deleted_at, timezone"),
    # Whose calendar is read, and whether: reconnecting a different Google
    # account rewrites the token on an active row without touching `status`.
    (
        "calendar_connections",
        "user_id",
        "mentor",
        "user_id, status, provider, refresh_token_encrypted",
    ),
)

LOG_FUNCTION = """
CREATE FUNCTION log_availability_change() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    ids uuid[] := '{}';
    value text;
    ends timestamptz;
    ended boolean := true;
BEGIN
    -- `set_updated_at` has already moved `updated_at`, so an UPDATE that
    -- changed nothing else still differs as a record; compare without it.
    IF TG_OP = 'UPDATE' AND (to_jsonb(OLD) - 'updated_at') = (to_jsonb(NEW) - 'updated_at') THEN
        RETURN NULL;
    END IF;
    IF TG_ARGV[1] = 'session' THEN
        IF TG_OP IN ('INSERT', 'UPDATE') THEN
            ends := (to_jsonb(NEW) ->> 'starts_at')::timestamptz
                + make_interval(mins => (to_jsonb(NEW) ->> 'duration_minutes')::int);
            ended := ended AND ends <= now();
        END IF;
        IF TG_OP IN ('UPDATE', 'DELETE') THEN
            ends := (to_jsonb(OLD) ->> 'starts_at')::timestamptz
                + make_interval(mins => (to_jsonb(OLD) ->> 'duration_minutes')::int);
            ended := ended AND ends <= now();
        END IF;
        IF ended THEN
            RETURN NULL;
        END IF;
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        value := to_jsonb(NEW) ->> TG_ARGV[0];
        IF value IS NOT NULL THEN ids := ids || value::uuid; END IF;
    END IF;
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        value := to_jsonb(OLD) ->> TG_ARGV[0];
        IF value IS NOT NULL THEN ids := ids || value::uuid; END IF;
    END IF;
    IF TG_ARGV[1] = 'session_type' THEN
        SELECT coalesce(array_agg(mentor_user_id), '{}') INTO ids
        FROM session_types WHERE id = ANY(ids);
    END IF;
    INSERT INTO mentor_availability_changes (mentor_user_id)
    SELECT DISTINCT user_id FROM mentor_profiles WHERE user_id = ANY(ids);
    RETURN NULL;
END
$$
"""


def _timestamps() -> list[sa.Column]:
    return [
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
    ]


def upgrade() -> None:
    """The cache, the log, the log function, nine triggers."""
    op.execute("SET lock_timeout = '3s'")

    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("mentor_user_id", sa.Uuid(), nullable=False),
        sa.Column("next_available_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("bookable_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("computed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mentor_next_availability")),
        sa.ForeignKeyConstraint(
            ["mentor_user_id"],
            ["mentor_profiles.user_id"],
            name=op.f("fk_mentor_next_availability_mentor_user_id_mentor_profiles"),
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "uq_mentor_next_availability_mentor_user_id", TABLE, ["mentor_user_id"], unique=True
    )
    op.execute(
        f"CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON {TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    op.create_table(
        LOG,
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("mentor_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mentor_availability_changes")),
        sa.ForeignKeyConstraint(
            ["mentor_user_id"],
            ["mentor_profiles.user_id"],
            name=op.f("fk_mentor_availability_changes_mentor_user_id_mentor_profiles"),
            ondelete="CASCADE",
        ),
    )
    # The card asks "any change for this mentor?" once per row it renders.
    op.create_index("ix_mentor_availability_changes_mentor_user_id", LOG, ["mentor_user_id"])

    op.execute(LOG_FUNCTION)
    for table, column, kind, columns in WATCHED:
        on_update = f"UPDATE OF {columns}" if columns else "UPDATE"
        op.execute(
            f"CREATE TRIGGER {TRIGGER} AFTER INSERT OR {on_update} OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {FUNCTION}('{column}', '{kind}')"
        )


def downgrade() -> None:
    """Drop the triggers, the function, then both tables.

    Nothing is lost that matters: every row is recomputable from the tables the
    triggers watch, which is what makes this a cache.
    """
    op.execute("SET lock_timeout = '3s'")
    for table, _column, _kind, _columns in WATCHED:
        op.execute(f"DROP TRIGGER IF EXISTS {TRIGGER} ON {table}")
    op.execute(f"DROP FUNCTION IF EXISTS {FUNCTION}()")
    op.drop_index("ix_mentor_availability_changes_mentor_user_id", table_name=LOG)
    op.drop_table(LOG)
    op.drop_index("uq_mentor_next_availability_mentor_user_id", table_name=TABLE)
    op.drop_table(TABLE)
