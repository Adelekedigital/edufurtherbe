"""M2 writes - a manual institution must name its creator

One CHECK, deferred here from PR 45 because nothing could create a
`source='manual'` row until this release shipped the write that does.

WHAT IT ASSERTS
===============
``source = 'manual'`` implies ``created_by IS NOT NULL``. A mirrored row has no
creator — the sync made it — and a user-created one always does, because the
education write sets it from the authenticated caller and there is no other path
to that value: the body cannot carry it, and search, which is public, only
reads.

WHY A CONSTRAINT AND NOT A COMMENT
==================================
It is the one invariant that says an anonymous caller never reached this table.
The review queue is built on it: an admin looking at a pending institution needs
to know who asked for it, and a null there is unanswerable rather than merely
untidy. If a future path ever creates a manual row without a creator, this fails
loudly at the write instead of producing a queue entry nobody can resolve.

The existing rows satisfy it already — every `manual` row today came from the
education write, and the mirrored 10,250 are `source='hipolabs'`.

Revision ID: a71c4d3e8b96
Revises: c41f7b2e9d05
Create Date: 2026-08-09 11:20:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a71c4d3e8b96"
down_revision: str | Sequence[str] | None = "c41f7b2e9d05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # `op.f` marks the name as already rendered: it stops alembic applying the
    # `ck_%(table_name)s_` convention a second time, and it puts the identifier
    # that actually reaches the database verbatim into source — which is what
    # `test_every_schema_identifier_appears_verbatim_in_source` looks for.
    op.create_check_constraint(
        op.f("ck_institutions_manual_names_its_creator"),
        "institutions",
        "source <> 'manual' OR created_by IS NOT NULL",
    )


def downgrade() -> None:
    """Downgrade schema.

    Fully reversible — dropping a CHECK loses no data, it only stops the
    database refusing a row that should never have been written.
    """
    # The **bare** name, as `create_check_constraint` took it. This project's
    # naming convention renders `ck_institutions_` itself, and passing the
    # rendered name gets it prefixed a second time —
    # `ck_institutions_ck_institutions_manual_names_its_creator`, which does not
    # exist. Caught by running the downgrade rather than by reading it.
    op.drop_constraint("manual_names_its_creator", "institutions", type_="check")
