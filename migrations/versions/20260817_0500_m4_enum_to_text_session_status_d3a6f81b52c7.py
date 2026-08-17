"""Settled decision #100, step eight: ``session_status``, and the constraint.

The last one. Three columns across two tables, and **four objects whose
definitions name the type** — three partial indexes and the exclusion constraint
that makes a mentor's overlapping live sessions impossible.

**The window where the constraint does not exist is a window where two bookings
both succeed.** That is why it is dropped and recreated here rather than
concurrently, and why the whole migration is one transaction: ``ALTER TABLE``
takes ``ACCESS EXCLUSIVE`` on ``sessions``, so no other session can insert
between the drop and the recreate — there is no window at all, only a lock. The
usual advice to avoid long exclusive locks is right and does not apply: a booking
that blocks for a moment is correct, and a booking that races past an absent
constraint is not.

``lock_timeout`` and ``statement_timeout`` are set for the same reason
`d7c31f8a2b45` set them: this runs against a populated database, ``ACCESS
EXCLUSIVE`` queues behind any long-running reader, and a queue on this table
stalls every read of the most-read table in the schema. Failing fast beats
stalling.

**Three of the four objects share one predicate**, so a single wrong
transcription breaks the double-booking guarantee *and* both upcoming-session
listings at once. ``LIVE_STATUSES`` is duplicated here per decision #43 — no
migration imports from ``app`` — and pinned by
``test_the_live_status_predicate_has_one_meaning``, which now compares four
copies. ``test_every_partial_index_predicate_survives_a_conversion`` compares
what actually landed.

``session_window()`` is unaffected: its signature is
``(timestamptz, integer) RETURNS tstzrange`` and its body never reads ``status``.
Only the *predicate* of the constraint that uses it does. Re-verified rather than
recalled.

**After this migration the schema has no PostgreSQL enum types at all**, and
``pg_enum`` is deleted rather than left unused — a helper that still existed
would be an invitation, and #100 would go back to being enforced by prose.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d3a6f81b52c7"
down_revision = "c2f9b6a41d83"
branch_labels = None
depends_on = None

#: Duplicated from `app.infra.db.models.sessions.LIVE_STATUSES`, deliberately —
#: decision #43, and the fourth copy in the chain. Pinned by
#: `test_the_live_status_predicate_has_one_meaning`, which fails when any copy
#: diverges. This predicate decides which sessions hold a mentor's booking slot,
#: and it is shared by three of the four objects below.
LIVE_STATUSES = "status IN ('pending_mentor_approval', 'confirmed')"

CONSTRAINT = "sessions_no_mentor_double_booking"

#: ``(table, column, type name, labels in declaration order, default or None)``.
#:
#: **The five-element shape is the template's, and it is load-bearing.** A first
#: draft here dropped the redundant type name — one type, three columns — and
#: `test_the_converted_enum_labels_have_one_meaning` failed: it parses this tuple
#: out of every migration by position, so a different shape reads `labels` from
#: the wrong slot. Copying the template means copying its shape.
CONVERSIONS: tuple[tuple[str, str, str, str, str | None], ...] = (
    (
        "sessions",
        "status",
        "session_status",
        "'pending_mentor_approval', 'confirmed', 'completed', 'cancelled', "
        "'declined', 'expired', 'no_show'",
        "pending_mentor_approval",
    ),
    (
        "session_events",
        "from_status",
        "session_status",
        "'pending_mentor_approval', 'confirmed', 'completed', 'cancelled', "
        "'declined', 'expired', 'no_show'",
        None,
    ),
    (
        "session_events",
        "to_status",
        "session_status",
        "'pending_mentor_approval', 'confirmed', 'completed', 'cancelled', "
        "'declined', 'expired', 'no_show'",
        None,
    ),
)

#: ``(name, table, columns, predicate)`` — the three partial indexes.
PREDICATE_INDEXES: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    ("ix_sessions_mentor_upcoming", "sessions", ("mentor_id", "starts_at"), LIVE_STATUSES),
    ("ix_sessions_mentee_upcoming", "sessions", ("mentee_id", "starts_at"), LIVE_STATUSES),
    (
        "ix_sessions_mentor_completed",
        "sessions",
        ("mentor_id",),
        "status = 'completed'",
    ),
)

ADD_CONSTRAINT = f"""
ALTER TABLE sessions
  ADD CONSTRAINT {CONSTRAINT}
  EXCLUDE USING gist (
    mentor_id WITH =,
    session_window(starts_at, duration_minutes) WITH &&
  ) WHERE ({LIVE_STATUSES})
"""

REWRITTEN = ("sessions", "session_events")


def _timeouts() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute("SET statement_timeout = '30s'")


def upgrade() -> None:
    _timeouts()

    # Raw SQL rather than `op.drop_constraint`: the naming convention has no `ex`
    # key, so this constraint carries a literal name, and running a literal name
    # through the convention is the double-prefix defect
    # `test_check_constraints_land_under_the_name_the_model_reports` exists for.
    op.execute(f"ALTER TABLE sessions DROP CONSTRAINT {CONSTRAINT}")

    for name, table, _columns, _predicate in PREDICATE_INDEXES:
        op.drop_index(name, table_name=table)

    for table, column, _type_name, labels, default in CONVERSIONS:
        if default is not None:
            op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} DROP DEFAULT")
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE text USING {column}::text")
        if default is not None:
            op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT '{default}'")
        op.create_check_constraint(f"{column}_is_known", table, f"{column} IN ({labels})")

    op.execute("DROP TYPE session_status")

    # The constraint first, so the guarantee is restored before anything else.
    op.execute(ADD_CONSTRAINT)

    for name, table, columns, predicate in PREDICATE_INDEXES:
        op.create_index(name, table, list(columns), postgresql_where=sa.text(predicate))

    for table in REWRITTEN:
        op.execute(f"ANALYZE {table}")


def downgrade() -> None:
    _timeouts()

    op.execute(f"ALTER TABLE sessions DROP CONSTRAINT {CONSTRAINT}")

    for name, table, _columns, _predicate in PREDICATE_INDEXES:
        op.drop_index(name, table_name=table)

    op.execute(
        "CREATE TYPE session_status AS ENUM ("
        "'pending_mentor_approval', 'confirmed', 'completed', 'cancelled', "
        "'declined', 'expired', 'no_show')"
    )

    for table, column, _type_name, _labels, default in reversed(CONVERSIONS):
        op.drop_constraint(f"{column}_is_known", table, type_="check")
        if default is not None:
            op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} DROP DEFAULT")
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} "
            f"TYPE session_status USING {column}::session_status"
        )
        if default is not None:
            op.execute(
                f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT '{default}'::session_status"
            )

    op.execute(ADD_CONSTRAINT)

    for name, table, columns, predicate in PREDICATE_INDEXES:
        op.create_index(name, table, list(columns), postgresql_where=sa.text(predicate))

    for table in REWRITTEN:
        op.execute(f"ANALYZE {table}")
