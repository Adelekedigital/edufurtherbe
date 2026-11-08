"""``push_subscriptions``: a browser's Web Push subscription, one row per device (#386).

- ``endpoint`` is ``UNIQUE`` across every user (``uq_push_subscriptions_endpoint``):
  one browser profile has one endpoint, so a second account signing in there
  takes the row over rather than both receiving that device's pushes.
- ``p256dh`` and ``auth`` are the keys a payload is encrypted to (RFC 8291).
- ``endpoint`` is ``https://`` printable ASCII, bounded in bytes so the unique
  index never meets its entry-size limit. The bounds are ``domain.push``'s,
  pinned by a test.
- ``user_id`` cascades: a subscription is only its owner's. The account purge
  anonymises rather than deletes, so the endpoints PR makes it delete these rows.
- ``ix_push_subscriptions_user_id`` finds a recipient's devices. Has
  ``updated_at``, so it gets the trigger (#23).

Expand only, and merged alone (project-conventions, *Migration ordering*):
nothing reads or writes this table until the next PR.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5b2e9d41c7a3"
down_revision: str | Sequence[str] | None = "ebd36a4e11df"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _now(name: str) -> sa.Column:
    return sa.Column(
        name, sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        "push_subscriptions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("p256dh", sa.Text(), nullable=False),
        sa.Column("auth", sa.Text(), nullable=False),
        _now("created_at"),
        _now("updated_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_push_subscriptions")),
        sa.UniqueConstraint("endpoint", name=op.f("uq_push_subscriptions_endpoint")),
        sa.CheckConstraint(
            "endpoint ~ '^https://[!-~]+$' AND octet_length(endpoint) <= 2048",
            name=op.f("ck_push_subscriptions_endpoint_shape"),
        ),
        sa.CheckConstraint(
            "char_length(p256dh) BETWEEN 1 AND 128",
            name=op.f("ck_push_subscriptions_p256dh_length"),
        ),
        sa.CheckConstraint(
            "char_length(auth) BETWEEN 1 AND 64", name=op.f("ck_push_subscriptions_auth_length")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_push_subscriptions_user_id_users"),
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_push_subscriptions_user_id", "push_subscriptions", ["user_id"])
    op.execute(
        "CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON push_subscriptions "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    """Drop the table. A subscription is re-made by the browser on its next
    opt-in, so nothing is lost that a user cannot restore."""
    op.execute("SET lock_timeout = '3s'")
    op.drop_table("push_subscriptions")
