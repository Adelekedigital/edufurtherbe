"""``calendar_connections`` — the mentor's grant to read when they are busy.

**Deferred since M1 and arriving now because it finally has a consumer.** Two
earlier migrations recorded its absence deliberately; settled decision #21 ships
a table with the phase that first needs it, and what needs it is free/busy
conflict detection, which lands next against this table.

**Not the same grant as the calendar the platform writes to.** ADR 0012 splits
them: ``calendar.app.created`` is granted **once by EduFurther's own account**
and creates every session's event — that is configuration, and it shipped
already. ``calendar.freebusy`` is granted **by each mentor**, reads only when
they are busy, and is what this table holds. Conflating the two is the belief
that made an earlier docstring claim the event writer needed this table; it
never did.

**Three divergences from the canonical DDL, each forced rather than chosen.**

``composio_auth_id`` is dropped. ADR 0012 supersedes ADR 0004's *"we stay on
Composio"*, so nothing mints one and the column would be null on every row
forever. The legacy ``composioAuthId`` values are separately recorded as
known-dead, so there is nothing to carry across either.

``provider`` and ``status`` are ``text`` + ``CHECK`` rather than the package's
enum types, per settled decision #100 — no PostgreSQL enum types remain and
adding two back would be the first.

``refresh_token`` is **added**, encrypted, and the package has no such column
because Composio held the tokens. We hold them now, so the table needs one.

**No ``legacy_bubble_id``.** Settled decision #27 puts it on tables derived from
their own Bubble Thing, and nothing migrates into this one — the only legacy
source is a dead Composio id. A column null on every row is not an anchor.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f4a83b1e07d2"
down_revision: str | Sequence[str] | None = "c3a917e4f6b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """One table, additive. Nothing existing changes shape."""
    op.create_table(
        "calendar_connections",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("external_account_id", sa.Text(), nullable=True),
        # **Encrypted, and the column name says so** — a reader who sees
        # `refresh_token` and writes a plaintext one has made a mistake nothing
        # would catch, because a string is a string.
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("last_synced_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "connected_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
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
        # CASCADE, per ADR 0013 and the package: a grant is meaningless without
        # the person who gave it, and a deleted user's token must not outlive
        # them by even one sweep.
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_calendar_connections_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_calendar_connections")),
        sa.CheckConstraint(
            "provider IN ('google')",
            name=op.f("ck_calendar_connections_provider_is_known"),
        ),
        # `revoked` is the state a disconnect reaches, kept rather than deleted
        # so *that they once connected* stays answerable — which matters when a
        # mentor asks why their calendar stopped being consulted.
        sa.CheckConstraint(
            "status IN ('active', 'revoked', 'error')",
            name=op.f("ck_calendar_connections_status_is_known"),
        ),
    )
    # **One active connection per provider per mentor**, verbatim from the
    # package. Partial, so a revoked row never blocks a reconnection — which is
    # the ordinary case: a mentor who disconnects and changes their mind.
    op.create_index(
        "uq_calendar_connections_active",
        "calendar_connections",
        ["user_id", "provider"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    # Settled decision #23: attached here rather than by a scanner.
    op.execute(
        "CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON calendar_connections "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    """Drops the table, and with it every mentor's grant.

    Irreversible in the way that matters: a token cannot be recovered from a
    dropped row, so every mentor would have to consent again. Stated rather than
    guarded, because a downgrade means the feature is going away in the same
    release and the consent would be worthless anyway.
    """
    op.execute("DROP TRIGGER IF EXISTS trg_set_updated_at ON calendar_connections")
    op.drop_index("uq_calendar_connections_active", table_name="calendar_connections")
    op.drop_table("calendar_connections")
