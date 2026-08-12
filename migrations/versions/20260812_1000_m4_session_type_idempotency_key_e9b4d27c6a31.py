"""M4 sessions - one live session type per mentor per name

The last table in this phase with no idempotency key, and the key is a product
invariant in its own right.

WHY IT IS NEEDED
================
Four of M4's five tables can already be upserted:

    sessions                      legacy_bubble_id UNIQUE
    session_type_booking_configs  UNIQUE (session_type_id)
    session_participants          UNIQUE (session_id, user_id)
    session_events                no key - the loader deletes its own rows first

`session_types` had neither, and **delete-then-insert is not available to it**:
`sessions.session_type_id` references it with `ON DELETE RESTRICT`, so on the
second run - which is the recovery plan rather than the exception - the delete
fails against the sessions the first run created.

WHY IT IS ALSO CORRECT ON ITS OWN
=================================
A mentor holding two *live* types with the same name is not a state worth being
able to represent: a mentee choosing between them cannot tell them apart, and
nothing downstream could say which was meant. The ETL made the gap visible; it
did not invent the invariant.

Partial on `deleted_at IS NULL`, so a retired type never blocks a new one with
the same name - the same shape as every other soft-delete predicate in this
schema.

THE GOTCHA, MEASURED
====================
`ON CONFLICT` infers a partial index only when the statement repeats the
predicate verbatim:

    ON CONFLICT (mentor_user_id, name) WHERE deleted_at IS NULL   -> works
    ON CONFLICT (mentor_user_id, name)                            -> refuses

The second raises `InvalidColumnReferenceError: there is no unique or exclusion
constraint matching the ON CONFLICT specification`. It does **not** silently pick
another index, which is the good news; it fails at runtime rather than at
declaration, which is the bad news, and on the second run rather than the first.
`test_the_upsert_requires_the_index_predicate` pins it.

WHAT THE GATE CANNOT SEE
========================
`alembic check` sees the index and is blind to its predicate and its partiality,
so the model and this file both declare it and the tests assert both halves
against `pg_indexes`.

Revision ID: e9b4d27c6a31
Revises: d7c31f8a2b45
Create Date: 2026-08-12 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e9b4d27c6a31"
down_revision: str | Sequence[str] | None = "d7c31f8a2b45"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_session_types_mentor_name"


def upgrade() -> None:
    """Upgrade schema."""
    # `session_types` is empty in this revision, so the build is instant. Set
    # anyway: this file runs against a populated database at cutover, where
    # CREATE INDEX takes a SHARE lock and blocks every write to the table for
    # its duration.
    op.execute("SET lock_timeout = '5s'")
    op.execute("SET statement_timeout = '30s'")

    op.create_index(
        INDEX,
        "session_types",
        ["mentor_user_id", "name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(INDEX, table_name="session_types")
