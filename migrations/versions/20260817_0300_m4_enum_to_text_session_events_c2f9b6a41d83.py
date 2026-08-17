"""Settled decision #100, step seven: ``session_events``' own two vocabularies.

``actor_type`` and ``reason_code``. Neither has an index dependency —
``ix_session_events_reason`` is partial on ``reason_code IS NOT NULL``, which
names no enum literal, so it rebuilds with the table rather than needing a drop
and recreate. `pg_depend` lists neither type against an index.

**This step deliberately does not touch ``from_status`` or ``to_status``**, the
other two enum columns on this table. Both are ``session_status``, which is
step 8: converting one column of a shared type and leaving the others is the
half-finished state the whole ordering exists to avoid, and it would leave
`sessions.status` and the two event columns disagreeing about their own type.
Four enum columns on one table, split across two migrations, on purpose.

``reason_code`` is nullable and stays that way. Legacy supplied no coded
cancellation field at all — ``Session Cancel/Decline Message`` is free text and
became ``reason_text`` — so every migrated cancellation event carries a null
code, which is why the index over it is partial in the first place. The ``IN``
constraint permits null without a special case.

``ActorType`` is the vocabulary that makes a null ``actor_id`` honest: ``SYSTEM``
and ``API`` are both actor-less, and an admin acting through the console is a
different fact from the same human acting as themselves. Constraining it matters
because the ETL writes these rows with hand-written SQL and never builds a model.
"""

from __future__ import annotations

from alembic import op

revision = "c2f9b6a41d83"
down_revision = "b8e1c5d70f42"
branch_labels = None
depends_on = None

#: ``(table, column, type name, labels in declaration order, default or None)``.
CONVERSIONS: tuple[tuple[str, str, str, str, str | None], ...] = (
    (
        "session_events",
        "actor_type",
        "actor_type",
        "'user', 'admin', 'system', 'api'",
        "user",
    ),
    (
        "session_events",
        "reason_code",
        "session_reason_code",
        "'mentor_unavailable', 'mentee_no_longer_needed', 'scheduling_conflict', "
        "'technical_issue', 'mentor_no_show', 'mentee_no_show', "
        "'expired_no_response', 'rescheduled', 'admin_action'",
        None,
    ),
)

REWRITTEN = ("session_events",)


def upgrade() -> None:
    for table, column, _type_name, labels, default in CONVERSIONS:
        if default is not None:
            op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} DROP DEFAULT")
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE text USING {column}::text")
        if default is not None:
            op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT '{default}'")
        op.create_check_constraint(f"{column}_is_known", table, f"{column} IN ({labels})")

    for _table, _column, type_name, _labels, _default in CONVERSIONS:
        op.execute(f"DROP TYPE {type_name}")

    # The rewrite discarded the table's statistics; see `a7d2f4b8c051`.
    for table in REWRITTEN:
        op.execute(f"ANALYZE {table}")


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

    for table in REWRITTEN:
        op.execute(f"ANALYZE {table}")
