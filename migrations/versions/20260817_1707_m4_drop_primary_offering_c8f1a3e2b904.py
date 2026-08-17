"""``primary_session_type_id`` is dropped, with its trigger.

**Measured before deciding: three consumers in production code, and after
`b4e9c72a1d58`, zero that need it.** `profile_store` read booking settings off
the primary and now reads the mentor column; `etl/sessions` set the pointer at
load; `models/mentoring` declared it and the guard.

D88 gave this column two jobs — *the offering a mentee lands on*, and *the source
an unconfigured offering falls back to*. The fallback lost its last member when
`requires_booking_confirmation` went back to `mentor_profiles`, and the first job
was never real: **nothing lands a mentee on it and nothing orders by it.**
``_live_session_types`` orders by ``name``, which is unique per mentor among live
rows and therefore a *total* order rather than a merely stable one. A session
type is active or inactive; mentees see what the mentor created, in name order.

**Two things stop being true, and both are the point rather than a cost.**

``default_meeting_venue`` leaves ``MentorProfileRead``. It was read through this
pointer, and venue has no mentor-level home to fall back to — so a client gets a
**missing field** rather than a null. A null would say *this mentor has no
venue*, which is a claim about the mentor; the truth is that venue is a property
of an offering. The owner sees it per offering in ``/me/session-types``.

``trg_refuse_retiring_a_primary_offering`` goes, and with it the sanctioned
two-step **release the pointer, then retire**. Deactivating or soft-deleting an
offering becomes a plain ``is_active`` toggle that nothing objects to. That is
what the ``PATCH`` and ``DELETE`` endpoints no longer have to translate into a
``409`` — the guard fired on ``UPDATE`` as well as delete, so without this the
``is_active`` toggle would have needed the same mapping or returned a 500.

**The drop order is forced.** A function cannot be dropped while a trigger
references it, so the trigger goes first; the foreign key goes before the column
it constrains. Written out rather than relying on ``CASCADE``, which would also
silently take anything else that happened to depend on either.

**The downgrade re-seeds the pointer.** Recreating the column, the key, the
function and the trigger restores the *mechanism* and leaves it inert: every
mentor's pointer would be null, so the guard would refuse nothing and
``default_meeting_venue`` would read null for everyone. That is not a reversal,
it is the same schema with the data thrown away. The backfill is
`d5a83b17c9e4`'s, unchanged — the mentor's earliest live offering by ``id``,
deterministic across runs and arbitrary among siblings created in the same tick,
which is acceptable because any live offering is a valid default and the mentor
replaces it deliberately.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8f1a3e2b904"
down_revision: str | Sequence[str] | None = "b4e9c72a1d58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FK_NAME = "fk_mentor_profiles_primary_session_type_id_session_types"

#: Restored verbatim from `d5a83b17c9e4`, which is where it was written and
#: reasoned about. Copied rather than imported: decision #43 forbids a migration
#: importing a live symbol, because editing it later would change what a shipped
#: revision does on replay.
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

#: The mentor's earliest live offering by `id`. See the module docstring for why
#: "earliest" is deterministic but arbitrary among same-tick siblings, and why
#: that is acceptable here.
RESEED_THE_POINTER = """
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


def upgrade() -> None:
    """Trigger, function, key, column — in that order, and none of it cascades."""
    op.execute("DROP TRIGGER IF EXISTS trg_refuse_retiring_a_primary_offering ON session_types")
    op.execute("DROP FUNCTION IF EXISTS refuse_retiring_a_primary_offering()")
    op.drop_constraint(FK_NAME, "mentor_profiles", type_="foreignkey")
    op.drop_column("mentor_profiles", "primary_session_type_id")


def downgrade() -> None:
    """Restore the column, the key, the guard — and the data that makes it live.

    The order mirrors ``upgrade`` reversed, and the re-seed sits **before** the
    trigger is created rather than after. The trigger fires on ``UPDATE`` of
    ``session_types`` and the backfill updates ``mentor_profiles``, so the
    ordering is not load-bearing today — but a guard installed before the rows it
    guards exist is the shape that bites the moment either statement grows.
    """
    op.add_column("mentor_profiles", sa.Column("primary_session_type_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        FK_NAME,
        "mentor_profiles",
        "session_types",
        ["primary_session_type_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.execute(RESEED_THE_POINTER)
    op.execute(GUARD_FUNCTION)
    op.execute(GUARD_TRIGGER)
