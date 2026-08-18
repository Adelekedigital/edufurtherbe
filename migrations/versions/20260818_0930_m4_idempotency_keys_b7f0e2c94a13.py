"""``idempotency_keys`` — a booking retried must not become two bookings.

A flaky mobile connection retrying ``POST /sessions`` creates two sessions, and
after payments it burns two charges. The client sends ``Idempotency-Key``; the
first request stores its answer here and every replay is served from it. See ADR
0024, landing in this pull request.

**Two divergences from the canonical package, and they are separate.**

``key text PRIMARY KEY`` becomes ``id uuid`` with the invariant re-declared as a
unique index. ADR 0015 admits no exception and non-negotiable #10 prescribes
exactly this resolution; the plan records it in advance so it is not re-argued.

The unique is on **``(user_id, key)``** rather than ``(key)``, which the plan
does not anticipate. It follows from the lookup being scoped to the caller,
which it must be: a row here holds a stored *response body*, so an unscoped read
would serve one user another user's booking. Once the read is scoped, a global
key space is a defect rather than a stricter rule — user B sending a key user A
already holds finds nothing on the scoped select and then collides on the
insert, a failure with no correct answer to give. Stripe scopes per account for
the same reason.

``NULLS NOT DISTINCT`` because ``user_id`` is nullable, as the package has it.
Every writer today is authenticated, so no row exists with a null — but under
the default ``NULLS DISTINCT`` two anonymous requests sharing a key would both
insert, which is the one thing this table exists to prevent, and leaving that
reachable for the first unauthenticated idempotent endpoint is how it ships
broken.

**Nothing is backfilled and nothing existing changes shape.** The table is
empty by definition: a key describes a request, and no request has been made.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7f0e2c94a13"
down_revision: str | Sequence[str] | None = "a2d64f87b1e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """One table, additive."""
    op.create_table(
        "idempotency_keys",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("response_body", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("locked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "expires_at",
            sa.TIMESTAMP(timezone=True),
            # Spelled the way PostgreSQL renders it, matching the model
            # verbatim. `interval '24 hours'` is the same value and normalises
            # to this, but the parity guard compares the declared text.
            server_default=sa.text("now() + '24:00:00'::interval"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # CASCADE, per ADR 0013's rule and the package: a key is meaningless
        # without the user who sent it, records no auditable fact, and expires
        # in twenty-four hours either way.
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_idempotency_keys_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_idempotency_keys")),
    )
    # **A unique index rather than a `UniqueConstraint`**, because `NULLS NOT
    # DISTINCT` is expressible on an index and the `uq` naming convention
    # renders on `column_0_name` alone — which would give this the same name as
    # any other `user_id`-leading unique on the table. Named by hand for the
    # same reason `session_types` names its composite foreign key by hand; that
    # convention has now produced a collision or an over-long identifier on five
    # tables this milestone.
    op.execute(
        "CREATE UNIQUE INDEX uq_idempotency_keys_user_key "
        "ON idempotency_keys (user_id, key) NULLS NOT DISTINCT"
    )
    op.create_index("ix_idempotency_keys_expires_at", "idempotency_keys", ["expires_at"])
    # Settled decision #23: attached here, in the migration that creates the
    # table, rather than by a scanner that re-triggers the whole schema.
    op.execute(
        "CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON idempotency_keys "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    """Drops the table, and with it every stored answer.

    That is the honest outcome and not a loss worth guarding: a key is valid for
    twenty-four hours, and a downgrade means the endpoint that issues them is
    going away in the same release.
    """
    op.execute("DROP TRIGGER IF EXISTS trg_set_updated_at ON idempotency_keys")
    op.drop_index("ix_idempotency_keys_expires_at", table_name="idempotency_keys")
    op.drop_index("uq_idempotency_keys_user_key", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")
