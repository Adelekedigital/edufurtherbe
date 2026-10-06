"""``feature_interest`` — "tell me when this ships", for any coming-soon thing.

Issue #365, asked for by the frontend on 2026-10-05 after a second **Notify me**
button was cut for want of somewhere to record the press. The first was Explore's
no-mentors state (#231), the second the Payments row on the mentor Integrations
page.

**`feature` is a bounded slug rather than an enum**, which is the whole point of
the ask: the next coming-soon control anywhere in the product works with no
backend release. The accepted cost is that a client typo stores a key nobody
notifies against — detectable by reporting distinct keys, not prevented.

Recording is feature-agnostic; **notifying is not**. Sending anything for a new
key needs a template id and a `Notification` member, so a button built on this
promises "we'll let you know" and never a date. `notified_at` is here so the
eventual send cannot tell one person twice.

The table is new and nothing reads it before this release, so both code versions
serve during the deploy. Expand only — there is no contract step.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1a7c3b94f28"
down_revision: str | Sequence[str] | None = "f2b8d4e71a63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "feature_interest"

#: The slug shape, as SQL. **A second representation of
#: `domain.interest.FEATURE_PATTERN`, pinned to it by a test** — the same
#: arrangement `SESSION_DURATION_MINUTES` has with its `CHECK`, and for the same
#: reason: a migration may not import application code, because the chain is
#: frozen and an edit to the domain would silently change what an old migration
#: meant.
#:
#: Worth having despite the copy: `scripts/` is a composition root that can
#: insert without passing through the API's validation, and this column's whole
#: value is that its contents are predictable.
FEATURE_SQL = "feature ~ '^[a-z][a-z0-9_]{1,39}$'"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("feature", sa.Text(), nullable=False),
        sa.Column("notified_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feature_interest")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_feature_interest_user_id_users"),
            # **CASCADE, matching `calendar_connections`.** An interest is a
            # standing request to be contacted; a deleted account cannot be, and
            # keeping the row would leave a promise nobody can keep.
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(FEATURE_SQL, name=op.f("ck_feature_interest_feature_is_a_slug")),
    )
    # **Idempotency lives here**, not in the writer: pressing the button twice is
    # an `ON CONFLICT DO NOTHING` against this, which is what makes a second
    # press cost nothing and need no `Idempotency-Key` header. It also serves the
    # caller's own read, `user_id` being leading.
    op.create_index("uq_feature_interest_user_feature", TABLE, ["user_id", "feature"], unique=True)
    # For the send that does not exist yet: who is still waiting for one feature.
    # Partial, because a notified row is never selected again.
    op.create_index(
        "ix_feature_interest_unnotified",
        TABLE,
        ["feature"],
        postgresql_where=sa.text("notified_at IS NULL"),
    )
    op.execute(
        f"CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON {TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    """Drop the table.

    Nothing derived from it and nothing else references it, so there is no
    salvage step: an interest is a request to be told about something, and a
    person whose row is gone simply has not asked. Losing them means a button
    they pressed is forgotten, which is the cost of reverting this and is
    recoverable only by their pressing it again.
    """
    op.drop_table(TABLE)
