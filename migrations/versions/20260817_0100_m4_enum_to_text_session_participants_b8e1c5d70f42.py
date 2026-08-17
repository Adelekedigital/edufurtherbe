"""Settled decision #100, step six: ``session_participants``.

Two columns, one table, and the first **unique** partial index to move.

``ix_session_participants_one_mentor`` is ``UNIQUE ... WHERE role = 'mentor'``,
and it is the only thing enforcing one mentor per session — the invariant that
catches drift between ``sessions.mentor_id`` and the participant rows. Its
predicate names an enum literal, so it cannot survive ``role`` changing type and
is dropped before the ``ALTER`` and recreated after, under the same name.

**A wrong predicate here is worse than a missing index**, which is why step 3's
predicate test exists. Rebuilt as, say, ``WHERE role = 'mentee'``, the index still
exists, is still unique, still carries its name, and `alembic check` still sees
an index by that name — while the "exactly one mentor per session" rule silently
stops applying to mentors and starts applying to mentees. Nothing else in the
gate compares a `WHERE` clause;
`test_every_partial_index_predicate_survives_a_conversion` compares the literals.

The two levels of "did not show up" stay two levels, and this migration does not
blur them: ``attendance_status`` is per person, and ``sessions.status = 'no_show'``
is per session. A mentee-attended, mentor-absent session has two participant rows
and exactly one session outcome. ``LEFT_EARLY`` has no legacy source at all —
Bubble records an arrival and never a departure — which is exactly the kind of
value #100 exists to keep removable.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b8e1c5d70f42"
down_revision = "a7d2f4b8c051"
branch_labels = None
depends_on = None

#: ``(table, column, type name, labels in declaration order, default or None)``.
CONVERSIONS: tuple[tuple[str, str, str, str, str | None], ...] = (
    (
        "session_participants",
        "role",
        "session_role",
        "'mentor', 'mentee', 'observer'",
        None,
    ),
    (
        "session_participants",
        "attendance_status",
        "attendance_status",
        "'pending', 'attended', 'no_show', 'left_early'",
        "pending",
    ),
)

#: ``(name, table, column, predicate, unique)``. `pg_depend`'s answer: only
#: `session_role` has an index dependency, and only this one index.
PREDICATE_INDEXES: tuple[tuple[str, str, str, str, bool], ...] = (
    (
        "ix_session_participants_one_mentor",
        "session_participants",
        "session_id",
        "role = 'mentor'",
        True,
    ),
)

REWRITTEN = ("session_participants",)


def upgrade() -> None:
    for name, table, _column, _predicate, _unique in PREDICATE_INDEXES:
        op.drop_index(name, table_name=table)

    for table, column, _type_name, labels, default in CONVERSIONS:
        if default is not None:
            op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} DROP DEFAULT")
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE text USING {column}::text")
        if default is not None:
            op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT '{default}'")
        op.create_check_constraint(f"{column}_is_known", table, f"{column} IN ({labels})")

    for _table, _column, type_name, _labels, _default in CONVERSIONS:
        op.execute(f"DROP TYPE {type_name}")

    for name, table, column, predicate, unique in PREDICATE_INDEXES:
        op.create_index(name, table, [column], unique=unique, postgresql_where=sa.text(predicate))

    # The rewrite discarded the table's statistics; see `a7d2f4b8c051`.
    for table in REWRITTEN:
        op.execute(f"ANALYZE {table}")


def downgrade() -> None:
    for name, table, _column, _predicate, _unique in PREDICATE_INDEXES:
        op.drop_index(name, table_name=table)

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

    for name, table, column, predicate, unique in PREDICATE_INDEXES:
        op.create_index(name, table, [column], unique=unique, postgresql_where=sa.text(predicate))

    for table in REWRITTEN:
        op.execute(f"ANALYZE {table}")
