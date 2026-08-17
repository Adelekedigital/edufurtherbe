"""Settled decision #100, step three: ``lookup_status``, the first shared type.

Two tables, one vocabulary. This is where the real cost of ``text`` + ``CHECK``
over a native enum shows up and is paid deliberately: **a ``CHECK`` cannot span
tables**, so ``institutions`` and ``scholarship_programs`` each carry their own,
and ``LookupStatus`` maps to a *set* of constraint names rather than one.

That is the trade #100 already weighed — a shared type gives one definition and
no way to remove a value; per-column constraints give a droppable vocabulary and
two places to keep in step. The two places are kept in step by
``test_every_converted_enum_has_a_check_naming_its_values``, which walks the
columns rather than the registry.

**And this is the first step with index predicates to move.** Both partial
indexes name an enum literal:

```
ix_institutions_pending          WHERE status = 'pending_review'::lookup_status
ix_scholarship_programs_pending  WHERE status = 'pending_review'::lookup_status
```

A predicate that names a type cannot survive its column changing type, so each is
dropped before the ``ALTER`` and recreated after, **under the same name**. The
recreated definition is *not* byte-identical to the old one and cannot be: the
literal is now ``'pending_review'::text``. Same name, same columns, same rows
matched — the cast is the only difference, and it is the whole point of the step.

The model side needed no edit. Both predicates were already written as plain
strings (``text("status = 'pending_review'")``), so only the rendered index in
the database moves.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e4b7d0c95a13"
down_revision = "d1f8a3c62b47"
branch_labels = None
depends_on = None

#: ``(table, column, type name, labels in declaration order, default or None)``.
#: Same shape as ``d1f8a3c62b47``, copied rather than imported — decision #105.
CONVERSIONS: tuple[tuple[str, str, str, str, str | None], ...] = (
    (
        "institutions",
        "status",
        "lookup_status",
        "'approved', 'pending_review', 'merged', 'rejected'",
        "approved",
    ),
    (
        "scholarship_programs",
        "status",
        "lookup_status",
        "'approved', 'pending_review', 'merged', 'rejected'",
        "approved",
    ),
)

#: ``(name, table, column, predicate)`` for the partial indexes whose predicate
#: names the type. Dropped before the conversion and recreated after.
#:
#: **These are `pg_depend`'s answer, not a reading of the predicates.** An index
#: appears as a dependency of an enum type only when its definition references
#: that type, which is exactly the set that cannot survive the column changing.
#: Reading predicates by eye is what produced the "three more indexes" error the
#: handoff records.
PREDICATE_INDEXES: tuple[tuple[str, str, str, str], ...] = (
    ("ix_institutions_pending", "institutions", "created_at", "status = 'pending_review'"),
    (
        "ix_scholarship_programs_pending",
        "scholarship_programs",
        "created_at",
        "status = 'pending_review'",
    ),
)


def upgrade() -> None:
    for name, table, _column, _predicate in PREDICATE_INDEXES:
        op.drop_index(name, table_name=table)

    # `lookup_status` is dropped once, after both columns are off it — unlike
    # step 2, where each type belonged to exactly one column.
    for table, column, _type_name, labels, default in CONVERSIONS:
        if default is not None:
            op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} DROP DEFAULT")
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE text USING {column}::text")
        if default is not None:
            op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT '{default}'")
        op.create_check_constraint(f"{column}_is_known", table, f"{column} IN ({labels})")

    op.execute("DROP TYPE lookup_status")

    for name, table, column, predicate in PREDICATE_INDEXES:
        op.create_index(name, table, [column], postgresql_where=sa.text(predicate))


def downgrade() -> None:
    for name, table, _column, _predicate in PREDICATE_INDEXES:
        op.drop_index(name, table_name=table)

    op.execute(
        "CREATE TYPE lookup_status AS ENUM ('approved', 'pending_review', 'merged', 'rejected')"
    )

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

    for name, table, column, predicate in PREDICATE_INDEXES:
        op.create_index(name, table, [column], postgresql_where=sa.text(predicate))
