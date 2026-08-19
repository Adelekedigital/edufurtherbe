"""``outbox_events`` — the intent to tell somebody, committed with the fact.

**Sending inside the request was the alternative, and it fails two ways at
once.** A booking would block on a third party, so a slow provider makes a slow
checkout; and a crash between the commit and the send loses the message with
nothing recording that it was owed. An outbox makes "this happened" and "this
person was told" one transaction and one retryable unit.

**No divergence from the canonical package.** ``08_features_platform.sql``
specifies this table and already gives it ``id uuid PRIMARY KEY DEFAULT
uuid_generate_v7()``, so ADR 0015 needs no exception here — the first table this
milestone where the package and the rule already agreed.

**``destination`` generalises rather than changes.** The package defaults it to
``'posthog'`` and describes analytics dispatch; the column answers *where does
this go*, and a channel is the same question. Notifications write ``'email'``
and the analytics consumer the package anticipates can still write ``'posthog'``
into the same table without either knowing about the other.

**No ``recipient_id`` column**, deliberately. It would be a fifth thing to keep
in step with ``payload``, and the drain resolves a recipient to an address at
send time anyway — so a user who changes their email between the enqueue and the
send is written to at the new one, which is the behaviour anybody would expect
and which a stored address would quietly get wrong.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d7b2c04e9f31"
down_revision: str | Sequence[str] | None = "c3a917e4f6b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """One table and one partial index. Nothing existing changes shape."""
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column(
            "payload",
            sa.dialects.postgresql.JSONB(),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("destination", sa.Text(), server_default=sa.text("'posthog'"), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("sent_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox_events")),
        # Verbatim from the package. `text` + `CHECK` is also what settled
        # decision #100 would have chosen independently, so there is nothing to
        # reconcile.
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'skipped')",
            name=op.f("ck_outbox_events_status_is_known"),
        ),
        # **No foreign key on `entity_id`.** The package has none and it is
        # right: the column points at whichever table `entity_type` names, so a
        # key could only reference one of them — and an outbox that cannot
        # describe a second entity type is an outbox for one feature.
    )
    # **Partial on pending**, where the package's is not. The drain asks for
    # exactly this and nothing else ever reads the table by date, so a full
    # index would be almost entirely rows the query discards — and this table
    # keeps every row it has ever written.
    op.create_index(
        "ix_outbox_events_pending",
        "outbox_events",
        ["created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    # Settled decision #23: attached here rather than by a scanner.
    op.execute(
        "CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON outbox_events "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    """Drops the table, and with it anything not yet sent.

    Honest rather than guarded: a downgrade means the senders are going away in
    the same release, so a pending row would have nowhere to go.
    """
    op.execute("DROP TRIGGER IF EXISTS trg_set_updated_at ON outbox_events")
    op.drop_index("ix_outbox_events_pending", table_name="outbox_events")
    op.drop_table("outbox_events")
