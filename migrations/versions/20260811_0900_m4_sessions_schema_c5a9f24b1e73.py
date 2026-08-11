"""M4 sessions - the merged booking, attendance, and the lifecycle log

`SessionBooking` (1,073) and `SessionTracker` (935) become one `sessions` table.
The split was a Bubble workaround rather than a domain distinction (package D3),
and that is measured here rather than quoted: across the 103 linked pairs in the
dev export the two rows agree on mentor, duration, cancellation flag, room name
and meeting venue **103 times out of 103**. Only `starts_at` differs, on 2 rows,
by +15 minutes and by -1 hour.

WHAT THIS MIGRATION IS AND IS NOT
=================================
Schema only. No transform, no loader, no endpoint. All five tables land empty.

The `no_mentor_double_booking` exclusion constraint is **not here** - it is the
next pull request, for the reason M3 split `b4e8d33a1c72` out of `a3f7c21d9e08`:
it is the guardrail this whole migration exists to make possible, and burying it
in a five-table diff is how it gets reviewed by nobody. Both revisions run
against empty tables, so adding it later costs nothing. On a *populated* table it
would mean cleaning every violating row first, and legacy has plenty: 19 mentor
and start-time pairs in the dev export are booked more than once, one of them 26
times.

FOUR OF THE PACKAGE'S NINE TABLES ARE DELIBERATELY ABSENT
=========================================================
`session_type_questions`, `session_type_question_options`, `intake_submissions`,
`intake_answers` - and `session_notes`, which the package marks as having no
Bubble source at all. None has a legacy source or a read surface in this phase,
and settled decision #21 ships a table with the phase that first needs it. The
intake stack is one unit and arrives whole rather than half-built.

DEPARTURES FROM THE PACKAGE, EACH DELIBERATE
============================================
1. `session_participants` takes a surrogate `id` where the package writes
   `PRIMARY KEY (session_id, user_id)`. ADR 0015 admits no exception, and the
   invariant that key carried survives as `uq_session_participants_session_id`
   over both columns. The rule was overridden twice before with every gate green,
   which is why `test_schema_parity` now walks the live schema for it.
2. `session_type_booking_configs` takes a surrogate `id` where the package makes
   `session_type_id` the primary key. Same record, same reasoning; the 1:1
   invariant survives as a `UNIQUE`.
3. `session_events` carries **no `updated_at` and no trigger**. The package gives
   it one while simultaneously calling the table "APPEND-ONLY. Revoke
   UPDATE/DELETE from the application role" - a contradiction rather than a
   decision. `mentor_status_events` already settled the shape in `f2a8c31b7e45`.
4. Index names take this repository's `ix_` prefix rather than the package's
   `idx_`, matching every index already in the chain.
5. `sessions.session_type_id` is **nullable**, and becomes `NOT NULL` in a later
   revision once the loader has given every migrated mentor an auto-created
   "General Mentorship" type. That is expand/contract across two releases; the
   contract half is not in this pull request.

DELETION POLICY - DERIVED FROM ADR 0013, NOT COPIED
===================================================
The package writes `ON DELETE CASCADE` on `session_participants.session_id`,
`session_events.session_id` and `session_types.mentor_user_id`. ADR 0013's rule
is cascade where the child is meaningless without its parent *and records no
auditable fact*, restrict where it is evidence. Two cascades survive that test -
the 1:1 booking config, and participant attendance, which is part of the session
record rather than an independent claim. Everything else restricts.

`session_events.session_id` restricting is the sharpest of these: the table is
the audit trail, and `test_no_cascade_path_reaches_a_table_that_must_be_retained`
names `sessions` and `session_events` as tables M4 must add to its retained set.
That test's own docstring predicts this exact failure - "M4 adds
`sessions.mentee_id ON DELETE CASCADE`, which is defensible... and `DELETE FROM
users` now destroys session history". Both `mentor_id` and `mentee_id` restrict.

WHAT THE GATE CANNOT SEE
========================
`alembic check` reads tables, columns, types and regular indexes. Outside that
here: three CHECK constraints, the predicates on five partial indexes, the
uniqueness of `ix_session_participants_one_mentor`, and the four triggers.
`test_sessions_schema` asserts each one, with an accepting case beside every
rejecting one.

Revision ID: c5a9f24b1e73
Revises: b4e8d33a1c72
Create Date: 2026-08-11 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c5a9f24b1e73"
down_revision: str | Sequence[str] | None = "b4e8d33a1c72"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The four tables carrying `updated_at`. `session_events` is **not** here, and
#: that is the point of listing them rather than looping over the new tables.
TRIGGERED_TABLES = (
    "session_types",
    "session_type_booking_configs",
    "sessions",
    "session_participants",
)

#: Created by hand so the matching `DROP TYPE` in downgrade is visible in this
#: file rather than implied. `meeting_provider` is absent: M2 created it for
#: `mentor_profiles.default_meeting_venue` and it is reused here.
ENUM_TYPES = {
    "session_status": (
        "pending_mentor_approval",
        "confirmed",
        "completed",
        "cancelled",
        "declined",
        "expired",
        "no_show",
    ),
    "session_role": ("mentor", "mentee", "observer"),
    "attendance_status": ("pending", "attended", "no_show", "left_early"),
    "session_reason_code": (
        "mentor_unavailable",
        "mentee_no_longer_needed",
        "scheduling_conflict",
        "technical_issue",
        "mentor_no_show",
        "mentee_no_show",
        "expired_no_response",
        "rescheduled",
        "admin_action",
    ),
    "actor_type": ("user", "admin", "system", "api"),
}

#: Written once. The same predicate guards three indexes here and the exclusion
#: constraint in the next revision - non-negotiable #8, and a predicate inside a
#: string is not a symbol any linter can bind.
LIVE_STATUSES = "status IN ('pending_mentor_approval', 'confirmed')"


def _enum(name: str) -> postgresql.ENUM:
    """Reference a type this migration already created, never redeclare it."""
    return postgresql.ENUM(*ENUM_TYPES[name], name=name, create_type=False)


def upgrade() -> None:
    """Upgrade schema."""
    # All five tables are empty, so nothing waits and no index build blocks a
    # write. Set anyway: this file runs against a populated database at cutover,
    # where a lock queue behind a long transaction stalls every SELECT on the
    # table rather than only this statement.
    op.execute("SET lock_timeout = '5s'")
    op.execute("SET statement_timeout = '30s'")

    for name, labels in ENUM_TYPES.items():
        rendered = ", ".join(f"'{label}'" for label in labels)
        op.execute(f"CREATE TYPE {name} AS ENUM ({rendered})")

    meeting_provider = postgresql.ENUM(
        "google_meet", "daily", "zoom", "custom", name="meeting_provider", create_type=False
    )

    op.create_table(
        "session_types",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("mentor_user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("application_stage", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
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
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_session_types")),
        sa.ForeignKeyConstraint(
            ["mentor_user_id"],
            ["mentor_profiles.user_id"],
            name=op.f("fk_session_types_mentor_user_id_mentor_profiles"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_session_types_created_by_users"),
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_session_types_mentor",
        "session_types",
        ["mentor_user_id"],
        postgresql_where=sa.text("is_active AND deleted_at IS NULL"),
    )

    op.create_table(
        "session_type_booking_configs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("session_type_id", sa.Uuid(), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column(
            "min_notice_minutes", sa.Integer(), server_default=sa.text("120"), nullable=False
        ),
        sa.Column("meeting_venue", meeting_provider, nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_session_type_booking_configs")),
        # Named for `column_0_name` by the convention, but spanning one column
        # only - this is the 1:1 invariant the package's primary key carried.
        sa.UniqueConstraint(
            "session_type_id", name=op.f("uq_session_type_booking_configs_session_type_id")
        ),
        sa.CheckConstraint(
            "duration_minutes BETWEEN 5 AND 480",
            name=op.f("ck_session_type_booking_configs_duration_minutes_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["session_type_id"],
            ["session_types.id"],
            name=op.f("fk_session_type_booking_configs_session_type_id_session_types"),
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("mentor_id", sa.Uuid(), nullable=False),
        sa.Column("mentee_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("session_type_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status",
            _enum("session_status"),
            server_default=sa.text("'pending_mentor_approval'"),
            nullable=False,
        ),
        sa.Column("starts_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("topic", sa.Text(), nullable=True),
        sa.Column("booking_message", sa.Text(), nullable=True),
        sa.Column("meeting_provider", meeting_provider, nullable=True),
        sa.Column("meeting_url", sa.Text(), nullable=True),
        sa.Column("external_room_id", sa.Text(), nullable=True),
        sa.Column("external_calendar_event_id", sa.Text(), nullable=True),
        sa.Column("rescheduled_from_session_id", sa.Uuid(), nullable=True),
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
        sa.Column("legacy_bubble_id", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
        sa.UniqueConstraint("legacy_bubble_id", name=op.f("uq_sessions_legacy_bubble_id")),
        sa.CheckConstraint("mentor_id <> mentee_id", name=op.f("ck_sessions_no_self_booking")),
        sa.CheckConstraint(
            "duration_minutes BETWEEN 5 AND 480", name=op.f("ck_sessions_duration_minutes_valid")
        ),
        sa.ForeignKeyConstraint(
            ["mentor_id"],
            ["users.id"],
            name=op.f("fk_sessions_mentor_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["mentee_id"],
            ["users.id"],
            name=op.f("fk_sessions_mentee_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_sessions_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["session_type_id"],
            ["session_types.id"],
            name=op.f("fk_sessions_session_type_id_session_types"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["rescheduled_from_session_id"],
            ["sessions.id"],
            name=op.f("fk_sessions_rescheduled_from_session_id_sessions"),
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_sessions_mentor_upcoming",
        "sessions",
        ["mentor_id", "starts_at"],
        postgresql_where=sa.text(LIVE_STATUSES),
    )
    op.create_index(
        "ix_sessions_mentee_upcoming",
        "sessions",
        ["mentee_id", "starts_at"],
        postgresql_where=sa.text(LIVE_STATUSES),
    )
    op.create_index(
        "ix_sessions_mentor_completed",
        "sessions",
        ["mentor_id"],
        postgresql_where=sa.text("status = 'completed'"),
    )
    op.create_index("ix_sessions_starts_at", "sessions", ["starts_at"])

    op.create_table(
        "session_participants",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", _enum("session_role"), nullable=False),
        sa.Column("joined_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("left_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "attendance_status",
            _enum("attendance_status"),
            server_default=sa.text("'pending'"),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_session_participants")),
        # Spans both columns; the convention names it for the first. This is the
        # invariant the package's composite primary key carried.
        sa.UniqueConstraint(
            "session_id", "user_id", name=op.f("uq_session_participants_session_id")
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name=op.f("fk_session_participants_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_session_participants_user_id_users"),
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_session_participants_one_mentor",
        "session_participants",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("role = 'mentor'"),
    )
    op.create_index("ix_session_participants_user", "session_participants", ["user_id"])

    op.create_table(
        "session_events",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("from_status", _enum("session_status"), nullable=True),
        sa.Column("to_status", _enum("session_status"), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column(
            "actor_type", _enum("actor_type"), server_default=sa.text("'user'"), nullable=False
        ),
        sa.Column("reason_code", _enum("session_reason_code"), nullable=True),
        sa.Column("reason_text", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_session_events")),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name=op.f("fk_session_events_session_id_sessions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name=op.f("fk_session_events_actor_id_users"),
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_session_events_session", "session_events", ["session_id", "created_at"])
    op.create_index(
        "ix_session_events_reason",
        "session_events",
        ["reason_code", "created_at"],
        postgresql_where=sa.text("reason_code IS NOT NULL"),
    )

    # Per table, in the migration that creates it (settled decision #23).
    # `session_events` is deliberately excluded - it has no `updated_at` to move,
    # and attaching one here would be the copy-paste this loop makes easy. A test
    # sweeps `pg_trigger` for every table that *does* carry the column.
    for table in TRIGGERED_TABLES:
        op.execute(
            f"CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )


def downgrade() -> None:
    """Downgrade schema.

    Fully reversible - all five tables land empty in this revision, so there is
    no data to lose. Dropped child-first, because every foreign key here
    restricts rather than cascades.
    """
    op.drop_index("ix_session_events_reason", table_name="session_events")
    op.drop_index("ix_session_events_session", table_name="session_events")
    op.drop_table("session_events")

    op.drop_index("ix_session_participants_user", table_name="session_participants")
    op.drop_index("ix_session_participants_one_mentor", table_name="session_participants")
    op.drop_table("session_participants")

    op.drop_index("ix_sessions_starts_at", table_name="sessions")
    op.drop_index("ix_sessions_mentor_completed", table_name="sessions")
    op.drop_index("ix_sessions_mentee_upcoming", table_name="sessions")
    op.drop_index("ix_sessions_mentor_upcoming", table_name="sessions")
    op.drop_table("sessions")

    op.drop_table("session_type_booking_configs")

    op.drop_index("ix_session_types_mentor", table_name="session_types")
    op.drop_table("session_types")

    # `meeting_provider` is deliberately not dropped: M2 created it and
    # `mentor_profiles.default_meeting_venue` still uses it.
    for name in ENUM_TYPES:
        op.execute(f"DROP TYPE {name}")
