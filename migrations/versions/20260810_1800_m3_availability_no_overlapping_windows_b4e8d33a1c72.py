"""M3 availability - a mentor's windows may not overlap on one weekday

Two windows overlapping on the same weekday carry no information their union
does not. `09:00-12:00` plus `10:00-13:00` means `09:00-13:00`, said twice — so
the pair is always a data-entry mistake rather than a state worth storing, and
the mentor who later edits one copy changes their availability in a way the
other copy silently undoes.

WHY IN THE DATABASE RATHER THAN THE WRITE PATH
==============================================
This is an invariant about the data, true on every path and forever, which is
the same category as uniqueness. Left to application code it would be written in
the write endpoint, again in the ETL, and again in whatever bulk editor comes
next — the duplication non-negotiable #8 exists for, and the shape this project
already shipped when `deleted_at IS NULL` was typed into five statements and
missed on the fifth. The existing guardrail putting overbooking in a constraint
rather than a check-then-insert is the same instinct one level down.

It is also only cheap **now**. Adding an exclusion constraint to a populated
table requires every existing row to satisfy it first; today both tables are
empty, so there is no backfill, no lock and no cleanup.

WHY A CUSTOM TYPE
=================
PostgreSQL ships range types over int, numeric, date and timestamp — and **not
over `time`**. `timerange` is declared here because the constraint needs it and
for no other reason.

HALF-OPEN, AND THAT IS LOAD-BEARING
===================================
`'[)'` means `09:00-12:00` and `12:00-14:00` touch without overlapping, so a
mentor can describe a continuous morning as two blocks — which is how the legacy
rows are shaped. With `'[]'` they would collide on the shared minute and real
data would be refused. `test_two_windows_that_only_touch_are_accepted` is the
test that fails if this is ever widened.

THE PREDICATE IS THE SUBTLE PART
================================
`WHERE (is_active AND deleted_at IS NULL)` keeps a switched-off or soft-deleted
window from blocking the slot it used to occupy — invisibly, because that row no
longer renders anywhere. Both halves have their own test, each with a fixture
holding *only* the excluded row, so the insert reaches the predicate instead of
being refused by a live neighbour.

WHAT THE ETL NOW OWES
=====================
The dev export has 6 weekdays carrying more than one row and **0 overlapping
pairs**, but that is 24 rows against production's 192. If production holds
overlaps, M3's loader must merge them into their union before insert — lossless,
because the union is what the two rows mean — and report every merge. That is a
required item on the ETL pull request rather than a hope.

`alembic check` is blind to exclusion constraints, as it is to CHECK constraints
and partial predicates. Six tests cover this one.

Revision ID: b4e8d33a1c72
Revises: a3f7c21d9e08
Create Date: 2026-08-10 18:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4e8d33a1c72"
down_revision: str | Sequence[str] | None = "a3f7c21d9e08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "availability_rules_no_overlap"

ADD_CONSTRAINT = f"""
ALTER TABLE availability_rules
  ADD CONSTRAINT {CONSTRAINT}
  EXCLUDE USING gist (
    mentor_user_id WITH =,
    day_of_week WITH =,
    timerange(start_time, end_time, '[)') WITH &&
  ) WHERE (is_active AND deleted_at IS NULL)
"""


def upgrade() -> None:
    """Upgrade schema."""
    # Empty table, so nothing waits and nothing is scanned. Set anyway, because
    # this file also runs against a populated database at cutover, where ADD
    # CONSTRAINT takes ACCESS EXCLUSIVE and a lock queue stalls every read.
    op.execute("SET lock_timeout = '5s'")
    op.execute("SET statement_timeout = '30s'")

    op.execute("CREATE TYPE timerange AS RANGE (subtype = time)")
    op.execute(ADD_CONSTRAINT)


def downgrade() -> None:
    """Downgrade schema.

    The type is dropped as well as the constraint. `DROP TABLE` does not remove
    a type and neither does `DROP CONSTRAINT`, so leaving it would make the next
    `upgrade` fail with "type timerange already exists" — the trap
    `f2a8c31b7e45` records for enums, which applies identically here.
    """
    op.execute(f"ALTER TABLE availability_rules DROP CONSTRAINT {CONSTRAINT}")
    op.execute("DROP TYPE timerange")
