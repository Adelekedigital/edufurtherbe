"""M4 sessions - a mentor may not hold two overlapping live sessions

The constraint this table exists for, and the one legacy could not express.

WHY LEGACY IS NOT THE BASELINE PEOPLE ASSUME
============================================
Bubble *did* prevent double-booking (settled decision #84): a day's bookings were
compared against the sessions table before a slot could be taken. But that logic
lived mostly in the **frontend**, so it could not see two people clicking in the
same second and was skipped entirely by any path that did not go through that
screen. `04_sessions.sql`'s heading "THE CONSTRAINT BUBBLE COULD NEVER ENFORCE"
is a claim about the *database*, and it is correct — this moves the control from
the browser to a place that can be neither raced nor bypassed.

THE PACKAGE'S EXPRESSION CANNOT BE BUILT
========================================
`04_sessions.sql` writes:

    EXCLUDE USING gist (
      mentor_id WITH =,
      tstzrange(starts_at, starts_at + (duration_minutes || ' minutes')::interval) WITH &&
    )

which fails outright:

    ERROR: functions in index expression must be marked IMMUTABLE

The cause is not the interval cast - three constructions were tried and all three
failed. `timestamptz + interval` is **STABLE**, because a day or month component
depends on the session's `TimeZone`. A `GENERATED ALWAYS` column fails for the
same reason.

WHY A FUNCTION AND NOT A STORED `ends_at`
=========================================
A stored end column also works, and is rejected. It is a derived value persisted,
which settled decision #56 forbids for the reason it gives - the value drifts the
moment `duration_minutes` is updated without it, silently, and keeping the two in
step needs a trigger, which is a second mechanism for one fact.

`session_window` has no such failure mode: there is nothing to keep in step.

THE `IMMUTABLE` LABEL IS EARNED, NOT ASSERTED
=============================================
Mislabelling a STABLE function as IMMUTABLE does not raise - it silently corrupts
the index, and a corrupt index here means accepted double-bookings.

Minutes are a fixed duration, so minutes-only arithmetic is genuinely
timezone-independent. Measured before relying on it: identical results across
UTC, America/New_York, Africa/Lagos, Pacific/Kiritimati and Australia/Lord_Howe,
over both US DST boundaries and the EU one - **20 combinations, 0 disagreements**.
The contrast is what confirms the diagnosis: the same expression with
`interval '1 day'` differs by an hour between zones over the same boundary, which
is exactly why PostgreSQL marks the generic operator STABLE.

Two tests hold it. One asserts `provolatile = 'i'`, which is only what was
declared; the other evaluates the function across four zones at a DST boundary,
which is the promise the declaration makes.

WHAT THE GATE CANNOT SEE
========================
`alembic check` is blind to functions **and** to exclusion constraints, so both
objects in this revision are invisible to it. `test_sessions_schema` asserts each
one, with an accepting case beside every rejecting one - and `[)` gets its own
test, because two sessions that merely touch must not collide.

Revision ID: d7c31f8a2b45
Revises: c5a9f24b1e73
Create Date: 2026-08-11 15:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d7c31f8a2b45"
down_revision: str | Sequence[str] | None = "c5a9f24b1e73"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "sessions_no_mentor_double_booking"
FUNCTION = "session_window"

#: Duplicated from `app.infra.db.models.sessions.LIVE_STATUSES`, deliberately.
#:
#: A migration is a historical artefact and no migration in this chain imports
#: from `app` - importing a live constant would let a later edit silently change
#: what an old revision does. So the copy stays, and decision #43's other remedy
#: applies: `test_the_live_status_predicate_has_one_meaning` fails when the two
#: diverge, on the pattern `EXPORT_TIMEZONE` already uses.
LIVE_STATUSES = "status IN ('pending_mentor_approval', 'confirmed')"

#: STRICT because null in, null out is the honest answer rather than an accident
#: - `duration_minutes` is NOT NULL, but the function is callable directly.
#: PARALLEL SAFE because it reads nothing and writes nothing.
CREATE_FUNCTION = f"""
CREATE FUNCTION {FUNCTION}(starts timestamptz, mins int)
RETURNS tstzrange
LANGUAGE sql
IMMUTABLE
STRICT
PARALLEL SAFE
AS $$
  SELECT tstzrange(starts, starts + (mins * interval '1 minute'))
$$
"""

ADD_CONSTRAINT = f"""
ALTER TABLE sessions
  ADD CONSTRAINT {CONSTRAINT}
  EXCLUDE USING gist (
    mentor_id WITH =,
    {FUNCTION}(starts_at, duration_minutes) WITH &&
  ) WHERE ({LIVE_STATUSES})
"""


def upgrade() -> None:
    """Upgrade schema."""
    # The table is empty in this revision, so nothing is scanned and nothing
    # waits. Set anyway: this file runs against a populated database at cutover,
    # where ADD CONSTRAINT takes ACCESS EXCLUSIVE and a lock queue behind a long
    # transaction stalls every read of the most-read table in the schema.
    op.execute("SET lock_timeout = '5s'")
    op.execute("SET statement_timeout = '30s'")

    op.execute(CREATE_FUNCTION)
    op.execute(ADD_CONSTRAINT)


def downgrade() -> None:
    """Downgrade schema.

    Constraint first, then the function: the constraint's index expression
    depends on it, and PostgreSQL refuses to drop a function something still
    uses. Leaving the function behind would also make the next `upgrade` fail
    with "function already exists" - the trap `b4e8d33a1c72` records for its
    range type and `f2a8c31b7e45` for enums, which applies identically to a
    function created without `OR REPLACE`.
    """
    op.execute(f"ALTER TABLE sessions DROP CONSTRAINT {CONSTRAINT}")
    op.execute(f"DROP FUNCTION {FUNCTION}(timestamptz, int)")
