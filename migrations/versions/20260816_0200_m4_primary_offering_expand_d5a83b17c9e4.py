"""M4 primary offering — expand only

WHAT THIS IS PART OF
====================
Settled decision #88 moves three fields off ``mentor_profiles`` and into
``session_type_booking_configs``, with ``mentor_profiles.primary_session_type_id``
naming the offering everything else falls back to. **This migration is the expand
half and nothing else**: it adds, it backfills, it drops nothing, and no reader
changes in the same release.

    this PR   add the column and the pointer, backfill, dual-write, add the guard
    next      switch readers to the new location
    last      drop the old columns from ``mentor_profiles``

Three releases because a column move is expand/contract, and CLAUDE.md's router
says so without qualification: *"Any schema change — expand/contract, never one
release."* Doing it in one would leave a rolling deploy with old code reading
columns the migration had already emptied.

THE BACKFILL IS NOT OPTIONAL, AND THAT WAS MEASURED
===================================================
``session_type_booking_configs.meeting_venue`` is nullable and null means
*inherit* (D21). Loaded from the real export, **all five configs are null** —
every one inherits from ``mentor_profiles.default_meeting_venue``:

The counts behind that are dev-export content and not worth quoting — that data
is junk-filled test data, where the *shape* survives and the values do not. The
shape is what matters here and it is structural: the ETL creates a config with a
duration and nothing else, so `meeting_venue` is null on every row it writes,
whatever the mentors happen to be.

After the move a config would inherit from the *primary* config, which is one of
those same nulls, so the whole chain would resolve to null. `SessionTypeRead`
declares ``meeting_venue`` **required** precisely because today's terminus cannot
be null (D92), so an unbackfilled move breaks a shipped response for every
mentor. Copying the mentor's value onto each config gives the chain a bottom
before any reader moves.

``custom_meeting_url`` IS DELIBERATELY NOT MOVED
================================================
It is written by nothing and read by nothing — not the ETL, not a write schema,
not any response. It exists as a column, a CHECK, and a test asserting it never
appears in a body. A column with no producer and no consumer should be dropped
rather than relocated, and that decision belongs to the contract step where the
CHECK goes with it.

Worth recording while it is in view, as a fact about the *constraint* rather
than about anybody's data: the CHECK is one-directional —
``custom_meeting_url IS NULL OR default_meeting_venue = 'custom'`` — so it forbids
a URL without a custom venue and **permits a custom venue with no URL**. A mentor
in that state is bookable with nowhere to hold the session. The dev export
contains such rows, but that export is junk-filled test data and proves only that
the state is *reachable*, which the constraint already told us. Not this
migration's to fix, and not something the move should carry forward unexamined.

THE POINTER IS NULLABLE, AND THE CYCLE IS WHY
=============================================
``mentor_profiles.primary_session_type_id`` references ``session_types``, whose
``mentor_user_id`` references ``mentor_profiles``. The cycle is at the table
level, not the row level: a profile is created with a null pointer, its offering
is created, then the pointer is set. A NOT NULL column could never be written.

Null is the ordinary state rather than an edge, and for a structural reason
rather than a count: a mentor profile is created by the M2 load and offerings
arrive with M4, so any mentor the sessions ETL never sees keeps a null pointer
permanently. The M4 transform creates exactly one "General Mentorship" type per
mentor it does see, so "primary" is unambiguous for those by construction — until
the product lets a mentor add a second.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5a83b17c9e4"
down_revision: str | Sequence[str] | None = "c4e91a7d3f52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Refuses to retire an offering while it is a mentor's primary.
#:
#: **`ON DELETE RESTRICT` cannot do this job.** Nothing hard-deletes a session
#: type — retirement is `deleted_at` or `is_active = false`, both of which are
#: UPDATEs, and a foreign key never sees an UPDATE. Decision #90 names the
#: trigger as the mechanism for exactly that reason.
#:
#: **This is the first business-rule trigger in the schema.** Every other trigger
#: here is `trg_set_updated_at`, which is infrastructure. Two consequences worth
#: stating: it must not be added to the list `timestamps_from_source` disables
#: during a load — that helper names `trg_set_updated_at` specifically, so it is
#: safe today and would stop being safe if anybody generalised it — and it needs
#: its own tests for both retirement paths, because neither is reachable by the
#: constraint it replaces.
GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION refuse_retiring_a_primary_offering()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF (NEW.deleted_at IS NOT NULL AND OLD.deleted_at IS NULL)
       OR (NEW.is_active = false AND OLD.is_active = true) THEN
        IF EXISTS (
            SELECT 1 FROM mentor_profiles
            WHERE primary_session_type_id = NEW.id
        ) THEN
            RAISE EXCEPTION
                'session type % is a mentor primary offering and cannot be retired', NEW.id
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$
"""

GUARD_TRIGGER = """
CREATE TRIGGER trg_refuse_retiring_a_primary_offering
BEFORE UPDATE ON session_types
FOR EACH ROW
EXECUTE FUNCTION refuse_retiring_a_primary_offering()
"""


def upgrade() -> None:
    """Upgrade schema — additive throughout."""
    op.add_column(
        "session_type_booking_configs",
        sa.Column(
            "requires_booking_confirmation",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column("mentor_profiles", sa.Column("primary_session_type_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_mentor_profiles_primary_session_type_id_session_types",
        "mentor_profiles",
        "session_types",
        ["primary_session_type_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # The venue first: null on every loaded config, and the chain needs a bottom
    # before any reader moves to it.
    op.execute(
        """
        UPDATE session_type_booking_configs c
           SET meeting_venue = mp.default_meeting_venue
          FROM session_types st
          JOIN mentor_profiles mp ON mp.user_id = st.mentor_user_id
         WHERE c.session_type_id = st.id
           AND c.meeting_venue IS NULL
        """
    )
    op.execute(
        """
        UPDATE session_type_booking_configs c
           SET requires_booking_confirmation = mp.requires_booking_confirmation
          FROM session_types st
          JOIN mentor_profiles mp ON mp.user_id = st.mentor_user_id
         WHERE c.session_type_id = st.id
        """
    )

    # The primary: the mentor's earliest live offering, by `id`.
    #
    # `uuid_generate_v7()` is time-ordered (ADR 0015), but **only to the
    # resolution of its timestamp and counter** — four offerings inserted in one
    # statement land in the same microsecond and the remaining bits are random,
    # measured rather than assumed. So "earliest" is *deterministic* (the same
    # rows order the same way on every run, which is what a re-runnable backfill
    # needs) and **arbitrary among siblings created in the same tick**.
    #
    # That is acceptable because any live offering is a valid default: this
    # chooses one so the column is never null where it could be set, and the
    # mentor replaces it deliberately. It would not be acceptable if the choice
    # carried meaning, which is the thing to remember if display order ever
    # depends on it.
    op.execute(
        """
        UPDATE mentor_profiles mp
           SET primary_session_type_id = (
               SELECT st.id FROM session_types st
                WHERE st.mentor_user_id = mp.user_id
                  AND st.deleted_at IS NULL
                  AND st.is_active
                ORDER BY st.id
                LIMIT 1
           )
        """
    )

    op.execute(GUARD_FUNCTION)
    op.execute(GUARD_TRIGGER)


def downgrade() -> None:
    """Downgrade schema.

    The backfilled values are left where they are: they are copies of columns
    that still exist and are still authoritative in this release, so removing
    them would discard nothing and restoring them costs nothing.
    """
    op.execute("DROP TRIGGER IF EXISTS trg_refuse_retiring_a_primary_offering ON session_types")
    op.execute("DROP FUNCTION IF EXISTS refuse_retiring_a_primary_offering()")
    op.drop_constraint(
        "fk_mentor_profiles_primary_session_type_id_session_types",
        "mentor_profiles",
        type_="foreignkey",
    )
    op.drop_column("mentor_profiles", "primary_session_type_id")
    op.drop_column("session_type_booking_configs", "requires_booking_confirmation")
