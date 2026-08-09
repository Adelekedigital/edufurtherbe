"""M2 mentor status events - the log becomes the write path

`mentor_profiles` recorded a mentor's *current* status and the *most recent*
decision about it, in the same row. That holds for one transition and breaks at
the third: a mentor declined, re-applied, approved, then unlisted has thrown
three of four decisions away, because each pair of columns can only describe the
last one.

An admin can now unlist an approved mentor without declining them, which is that
third transition.

WHAT LIVES WHERE
================
**Current state stays on `mentor_profiles`** — `approval_status` and
`listing_status`. Not because deriving them would be slow at 44 mentors (it
would not), but because `ix_mentor_profiles_searchable` is a partial index on
exactly those two columns, and a constraint like "listed implies approved" can
be written over columns and not over "the state implied by the newest row in
another table".

**Every transition lives in `mentor_status_events`** — what changed, who, when,
why. One row states one fact and copies nothing forward, so two concurrent
transitions on different dimensions cannot record a state that never existed.

THE TRIGGER IS THE POINT
========================
`trg_apply_mentor_status` maps an inserted event onto the column it concerns.
Application code writes **only the event** and never touches the status columns.

This is deliberately structural rather than conventional. The alternative — one
helper function everybody remembers to call — is what this repository has been
burned by four times: `deleted_at IS NULL` hand-typed into five statements and
missed on the fifth, then missed again in `list_awards` two modules later. A
trigger makes forgetting impossible instead of discouraged, and
`trg_set_updated_at` is the same pattern already in this schema.

`alembic check` is blind to triggers. `test_mentor_status_log` inserts an event
directly and asserts the column moved, which is the only thing that proves the
trigger rather than the caller.

THE BACKFILL INVENTS NOTHING
============================
One event per existing profile, from its current state, carrying `approved_at`
and `approved_by` where they exist. A **pending** mentor gets no event at all —
writing an `approved` row for one would fabricate a decision nobody made, and
the queue would then show a history it never had.

`updated_at` is held off across the backfill. Every migrated mentor's row is
touched by the trigger, and without that hold their timestamps would all move to
the migration clock — the same defect the catalogue sync shipped and fixed.

WHAT GOES
=========
`approved_at`, `approved_by`, `declined_at`, `declined_by`, `decline_reason`,
`unlisted_at`, `unlisted_reason` — each now a duplicate of the newest event, and
a copy that can drift is the defect non-negotiable #8 names.

`ix_mentor_profiles_unlisted` goes with them, and is **not** replaced: it indexed
`(unlisted_reason, unlisted_at)` to answer "who is unlisted, why, and when",
which is now a query against the log and served by the log's own index.
`ix_mentor_profiles_searchable` already covers `listing_status`.

Revision ID: f2a8c31b7e45
Revises: e7d1a94c2b60
Create Date: 2026-08-10 09:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f2a8c31b7e45"
down_revision: str | Sequence[str] | None = "e7d1a94c2b60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DROPPED = (
    "approved_at",
    "approved_by",
    "declined_at",
    "declined_by",
    "decline_reason",
    "unlisted_at",
    "unlisted_reason",
)

# The whole projection, in one function.
#
# Approval values touch `approval_status` and listing values touch
# `listing_status`, never both — that separation is the reason one row can state
# one fact without copying the other dimension forward.
APPLY_STATUS = """
CREATE OR REPLACE FUNCTION apply_mentor_status() RETURNS trigger AS $$
BEGIN
    -- `::text::` in between, deliberately. PostgreSQL refuses a direct cast
    -- between two enum types — "cannot cast type mentor_status_type to
    -- listing_status" — and the trigger silently did nothing useful until an
    -- event was inserted *directly* rather than through a caller that also
    -- wrote the column. That is the whole reason the test inserts one by hand.
    IF NEW.status_type IN ('approved', 'declined') THEN
        UPDATE mentor_profiles
           SET approval_status = NEW.status_type::text::approval_status
         WHERE user_id = NEW.mentor_user_id;
    ELSE
        UPDATE mentor_profiles
           SET listing_status = NEW.status_type::text::listing_status
         WHERE user_id = NEW.mentor_user_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    """Upgrade schema."""
    # Created by hand, and referenced with `create_type=False` below — the
    # pattern the lookups migration records: without it `create_table` emits a
    # second `CREATE TYPE` and fails, and the matching `DROP TYPE` in downgrade
    # would be implied rather than visible in this file.
    op.execute(
        "CREATE TYPE mentor_status_type AS ENUM ('approved', 'declined', 'listed', 'unlisted')"
    )
    status_type = postgresql.ENUM(
        "approved", "declined", "listed", "unlisted", name="mentor_status_type", create_type=False
    )

    op.create_table(
        "mentor_status_events",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        # References `mentor_profiles(user_id)`, not `users(id)` — the same
        # decision `mentor_service_offerings` records: same value, different
        # guarantee, and an event cannot attach to a user with no profile.
        sa.Column("mentor_user_id", sa.Uuid(), nullable=False),
        sa.Column("status_type", status_type, nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        # Null only for the backfill, where nobody made the decision. Named for
        # the house convention — `created_by`, `granted_by`, `approved_by` —
        # rather than the event-sourcing word for the same thing.
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mentor_status_events")),
        sa.ForeignKeyConstraint(
            ["mentor_user_id"],
            ["mentor_profiles.user_id"],
            name=op.f("fk_mentor_status_events_mentor_user_id_mentor_profiles"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_mentor_status_events_created_by_users"),
            ondelete="RESTRICT",
        ),
    )
    # The only read is one mentor's history, newest first.
    op.create_index(
        "ix_mentor_status_events_mentor",
        "mentor_status_events",
        ["mentor_user_id", sa.text("created_at DESC")],
    )

    op.execute(APPLY_STATUS)
    op.execute(
        "CREATE TRIGGER trg_apply_mentor_status "
        "AFTER INSERT ON mentor_status_events "
        "FOR EACH ROW EXECUTE FUNCTION apply_mentor_status()"
    )

    # Backfill. `session_replication_role` suppresses the trigger *and*
    # `trg_set_updated_at` for the duration: the profiles already hold the state
    # these events describe, so re-applying it would move every migrated
    # mentor's `updated_at` to the migration clock.
    op.execute("SET session_replication_role = replica")
    op.execute(
        """
        INSERT INTO mentor_status_events
            (mentor_user_id, status_type, reason, created_by, created_at)
        SELECT user_id,
               approval_status::text::mentor_status_type,
               decline_reason,
               COALESCE(approved_by, declined_by),
               COALESCE(approved_at, declined_at, created_at)
          FROM mentor_profiles
         WHERE approval_status <> 'pending'
        """
    )
    op.execute(
        """
        INSERT INTO mentor_status_events
            (mentor_user_id, status_type, reason, created_by, created_at)
        SELECT user_id,
               listing_status::text::mentor_status_type,
               -- Only on an `unlisted` event. `unlisted_reason` carries a
               -- server default of `never_approved`, so **every** profile holds
               -- a value whether it is listed or not — copying it blindly put
               -- "never_approved" on the listing event of an approved, listed
               -- mentor, which is a history that never happened.
               CASE WHEN listing_status = 'unlisted' THEN unlisted_reason::text END,
               NULL,
               COALESCE(unlisted_at, created_at)
          FROM mentor_profiles
         WHERE approval_status <> 'pending'
        """
    )
    op.execute("SET session_replication_role = DEFAULT")

    op.drop_index("ix_mentor_profiles_unlisted", table_name="mentor_profiles")
    for column in DROPPED:
        op.drop_column("mentor_profiles", column)


def downgrade() -> None:
    """Downgrade schema.

    The columns come back empty. Their values live in the events, and putting
    them back means picking one event per mentor per dimension — which is the
    derivation this migration exists to stop doing. The log is not dropped, so
    nothing is lost; it is simply no longer projected onto columns.
    """
    op.add_column(
        "mentor_profiles", sa.Column("approved_at", sa.TIMESTAMP(timezone=True), nullable=True)
    )
    op.add_column("mentor_profiles", sa.Column("approved_by", sa.Uuid(), nullable=True))
    op.add_column("mentor_profiles", sa.Column("decline_reason", sa.Text(), nullable=True))
    op.add_column(
        "mentor_profiles", sa.Column("declined_at", sa.TIMESTAMP(timezone=True), nullable=True)
    )
    op.add_column("mentor_profiles", sa.Column("declined_by", sa.Uuid(), nullable=True))
    op.add_column(
        "mentor_profiles", sa.Column("unlisted_at", sa.TIMESTAMP(timezone=True), nullable=True)
    )
    op.add_column(
        "mentor_profiles",
        sa.Column(
            "unlisted_reason",
            postgresql.ENUM(name="unlisted_reason", create_type=False),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        op.f("fk_mentor_profiles_approved_by_users"),
        "mentor_profiles",
        "users",
        ["approved_by"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        op.f("fk_mentor_profiles_declined_by_users"),
        "mentor_profiles",
        "users",
        ["declined_by"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_mentor_profiles_unlisted",
        "mentor_profiles",
        ["unlisted_reason", "unlisted_at"],
        postgresql_where=sa.text("listing_status = 'unlisted'"),
    )

    op.execute("DROP TRIGGER IF EXISTS trg_apply_mentor_status ON mentor_status_events")
    op.execute("DROP FUNCTION IF EXISTS apply_mentor_status()")
    op.drop_index("ix_mentor_status_events_mentor", table_name="mentor_status_events")
    op.drop_table("mentor_status_events")
    op.execute("DROP TYPE mentor_status_type")
