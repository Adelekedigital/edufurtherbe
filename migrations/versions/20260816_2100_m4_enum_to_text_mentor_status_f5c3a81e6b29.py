"""Settled decision #100, step four: the mentor status cluster, and its trigger.

**Three columns across two tables in one migration, and that grouping is the
whole reason this step exists.** `apply_mentor_status` reads
`mentor_status_events.status_type` and writes `mentor_profiles.approval_status`
and `.listing_status`. The original plan put `mentor_status_events` with the
single-column tables in step 2 and never named the other two at all.

**A plpgsql body carries no dependency records.** `DROP TYPE approval_status`
succeeds while a function still names it: the migration applies, every migration
test passes, `alembic check` reports no drift, and the trigger dies at the next
insert with *type "approval_status" does not exist* — on the write path every
approval and every unlisting goes through. Nothing in the gate sees it, which is
why the function is rewritten in the same migration rather than a later one.

The rewrite is a **simplification**, which is the tell that the grouping is
right. The old body carried a double cast:

```sql
SET approval_status = NEW.status_type::text::approval_status
```

`::text::` was there because PostgreSQL refuses a direct cast between two enum
types — the function's own comment records that the first version silently did
nothing until an event was inserted by hand. With all three columns `text` the
cast has nothing left to do, and the hack disappears along with the types that
forced it.

**`ix_mentor_profiles_searchable` is not touched.** It indexes both converted
columns, but its predicate is `deleted_at IS NULL` and names no enum literal, so
it rebuilds with the table. `pg_depend` confirms it: neither type has an index
dependency.
"""

from __future__ import annotations

from alembic import op

revision = "f5c3a81e6b29"
down_revision = "e4b7d0c95a13"
branch_labels = None
depends_on = None

#: ``(table, column, type name, labels in declaration order, default or None)``.
CONVERSIONS: tuple[tuple[str, str, str, str, str | None], ...] = (
    (
        "mentor_profiles",
        "approval_status",
        "approval_status",
        "'pending', 'approved', 'declined'",
        "pending",
    ),
    (
        "mentor_profiles",
        "listing_status",
        "listing_status",
        "'listed', 'unlisted'",
        "unlisted",
    ),
    (
        "mentor_status_events",
        "status_type",
        "mentor_status_type",
        "'approved', 'declined', 'listed', 'unlisted'",
        None,
    ),
)

#: Text columns throughout, so the assignment is direct. Kept as
#: ``CREATE OR REPLACE`` rather than drop-and-recreate: the trigger stays
#: attached, so there is no window in which an event lands unprojected.
APPLY_MENTOR_STATUS_TEXT = """
CREATE OR REPLACE FUNCTION apply_mentor_status() RETURNS trigger AS $$
BEGIN
    -- No cast at all now. `::text::` was here because PostgreSQL refuses a
    -- direct cast between two enum types, and settled decision #100 removed both
    -- enums; `status_type`, `approval_status` and `listing_status` are all text.
    -- The CHECK on each column is what refuses a value the vocabulary lacks.
    IF NEW.status_type IN ('approved', 'declined') THEN
        UPDATE mentor_profiles
           SET approval_status = NEW.status_type
         WHERE user_id = NEW.mentor_user_id;
    ELSE
        UPDATE mentor_profiles
           SET listing_status = NEW.status_type
         WHERE user_id = NEW.mentor_user_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

#: The pre-#100 body, restored verbatim by ``downgrade``.
APPLY_MENTOR_STATUS_ENUM = """
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
    for table, column, _type_name, labels, default in CONVERSIONS:
        if default is not None:
            op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} DROP DEFAULT")
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE text USING {column}::text")
        if default is not None:
            op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT '{default}'")
        op.create_check_constraint(f"{column}_is_known", table, f"{column} IN ({labels})")

    # **Before the DROP TYPE, not after.** The function is the only thing still
    # naming these types, and nothing would stop the drop if it were left
    # pointing at them — the failure would wait for the next insert.
    op.execute(APPLY_MENTOR_STATUS_TEXT)

    for _table, _column, type_name, _labels, _default in CONVERSIONS:
        op.execute(f"DROP TYPE {type_name}")


def downgrade() -> None:
    for _table, _column, type_name, labels, _default in CONVERSIONS:
        op.execute(f"CREATE TYPE {type_name} AS ENUM ({labels})")

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

    op.execute(APPLY_MENTOR_STATUS_ENUM)
