"""Settled decision #100, step five: ``meeting_provider``.

Two columns, two tables, one type. No index predicate names it, no function
reads it, and only one of the two columns carries a default — so this is the
plain multi-column case, and the last quiet step before the session vocabularies.

**One column is nullable, and the constraint needs no special case for it.**
``sessions.meeting_provider`` is null until a venue is decided, and
``meeting_provider IN (...)`` permits that: ``NULL IN (...)`` evaluates to
unknown, and a ``CHECK`` rejects only what is *false*. Writing
``meeting_provider IS NULL OR meeting_provider IN (...)`` would be the same
constraint spelled longer, and the shorter form is what
``check_is_known`` renders for every column, nullable or not.

**Why this step matters beyond the conversion.** ``MeetingProvider.CUSTOM`` has
nowhere to keep a URL — ``mentor_profiles.custom_meeting_url`` was removed by
D88's contract step because nothing had ever written it — and one migrated
offering sits on it. ``ZOOM`` has no legacy source at all. Both are values the
schema could not shed while this was a PostgreSQL enum, because there is no
``ALTER TYPE ... DROP VALUE``. After this migration, dropping either is one
``CHECK`` swap, which is the entire point of #100.
"""

from __future__ import annotations

from alembic import op

revision = "a7d2f4b8c051"
# Re-pointed at the head when this landed. It was written against
# `f5c3a81e6b29` — enum step four — and the booking-notice migration merged in
# between, so leaving it there would have given the chain two heads and made
# `alembic upgrade head` refuse. That migration touches `min_notice_minutes` and
# this one touches `meeting_venue`, so the order between them means nothing
# beyond which landed first.
down_revision = "f1b6a92c7d4e"
branch_labels = None
depends_on = None

#: ``(table, column, type name, labels in declaration order, default or None)``.
CONVERSIONS: tuple[tuple[str, str, str, str, str | None], ...] = (
    (
        "session_type_booking_configs",
        "meeting_venue",
        "meeting_provider",
        "'google_meet', 'daily', 'zoom', 'custom'",
        "google_meet",
    ),
    (
        "sessions",
        "meeting_provider",
        "meeting_provider",
        "'google_meet', 'daily', 'zoom', 'custom'",
        None,
    ),
)


def upgrade() -> None:
    for table, column, _type_name, labels, default in CONVERSIONS:
        if default is not None:
            op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} DROP DEFAULT")
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE text USING {column}::text")
        if default is not None:
            op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT '{default}'")
        op.create_check_constraint(f"{column}_is_known", table, f"{column} IN ({labels})")

    # Once, after both columns are off it.
    op.execute("DROP TYPE meeting_provider")

    # **`ALTER COLUMN ... TYPE` rewrites the table and discards its statistics**,
    # and the planner will not choose a partial index it can no longer cost.
    # Caught by `test_the_completed_count_uses_its_partial_index`, which asserts
    # the completed-count query reaches `ix_sessions_mentor_completed`: after the
    # rewrite it took `ix_sessions_mentor_window` instead.
    #
    # That test only exists for `sessions`, so steps 2 to 4 rewrote their tables
    # with nothing watching. It matters least there — `users` is 43 rows and
    # autovacuum catches up quickly — and most here and in steps 6 and 8, which
    # rewrite the tables that actually grow. Waiting for autovacuum means serving
    # worse plans in the window right after the deploy, which is the window a
    # migration is under the most scrutiny.
    #
    # `ANALYZE` is permitted inside a transaction block; `VACUUM` is not, and is
    # not what is needed — the rewrite already reclaimed the space.
    for table in ("session_type_booking_configs", "sessions"):
        op.execute(f"ANALYZE {table}")


def downgrade() -> None:
    op.execute("CREATE TYPE meeting_provider AS ENUM ('google_meet', 'daily', 'zoom', 'custom')")

    for table, column, type_name, _labels, default in reversed(CONVERSIONS):
        op.drop_constraint(f"{column}_is_known", table, type_="check")
        if default is not None:
            op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} DROP DEFAULT")
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} "
            f"TYPE {type_name} USING {column}::{type_name}"
        )
        if default is not None:
            op.execute(
                f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT '{default}'::{type_name}"
            )

    # The downgrade rewrites the same two tables, so it owes the same ANALYZE.
    for table in ("session_type_booking_configs", "sessions"):
        op.execute(f"ANALYZE {table}")
