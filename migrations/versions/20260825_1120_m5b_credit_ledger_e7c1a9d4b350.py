"""The credit ledger — ``credit_lots`` and ``credit_transactions``.

Two new tables with no writer yet, so nothing existing can violate them and
``downgrade`` drops exactly what ``upgrade`` made.

**Five sources and five reasons, each with a producer.** The canonical DDL
declares seven sources; settled decision #21 names ``credit_source`` as its own
cautionary example, because it "contains ``purchase`` while payments are out of
scope by decision #8". Also absent: ``session_no_show_forfeit``, which reads as a
transaction and is not one — the credit left the balance when the session was
booked, and not refunding it is the *absence* of a row.

``opening_balance`` is a source the package does not have. Calling a migrated
balance ``monthly_free`` would assert a cadence the source never had: the legacy
renewal was a per-user scheduled workflow (``bookingCredit-WFCode``, populated on
27 of 43 dev users), and its dates land on thirteen different days of the month.

**Foreign keys ``RESTRICT``, where the package says ``CASCADE``.** ADR 0013:
restrict where the child is evidence.

**``credit_transactions`` gets no ``trg_set_updated_at``**, because it has no
``updated_at`` to maintain. That omission is deliberate and *invisible*:
``CREATE TRIGGER`` does not validate the function body, so attaching one here
would succeed and fail only at the first ``UPDATE`` — which an append-only table
never receives. The schema test asserts the trigger's absence for that reason.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7c1a9d4b350"
down_revision: str | Sequence[str] | None = "c9e4b1f78d02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LOTS = "credit_lots"
TRANSACTIONS = "credit_transactions"

#: Written out rather than imported: **no migration imports from ``app``**, so a
#: later edit to the enum cannot silently rewrite what already shipped.
#: `test_every_converted_enum_has_a_check_naming_its_values` compares these
#: literals against the classes, which is what keeps the two honest.
SOURCES = "'profile_completed', 'referral_unlock', 'monthly_free', 'refund', 'opening_balance'"
REASONS = (
    "'grant', 'session_booked', 'session_cancelled_refund', 'session_no_show_refund', 'lot_expired'"
)

#: The refund pair, spelled out for the same reason. The partial index below is
#: only correct because it names *these two* rather than every reason carrying a
#: session.
REFUND_REASONS = "'session_cancelled_refund', 'session_no_show_refund'"


def upgrade() -> None:
    """Create both tables, their six CHECKs, four indexes, one UNIQUE and one trigger."""
    op.create_table(
        LOTS,
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        # `smallint`: the largest grant this product produces is three, and the
        # opening balance's ceiling is the legacy maximum of five.
        sa.Column("quantity_granted", sa.SmallInteger(), nullable=False),
        sa.Column("quantity_remaining", sa.SmallInteger(), nullable=False),
        # Null means never. Only the starter holds one today; every purchased lot
        # joins it when payments land, which is why the read filter is written
        # against NULL rather than a sentinel date.
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credit_lots")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_credit_lots_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "quantity_granted > 0", name=op.f("ck_credit_lots_quantity_granted_positive")
        ),
        sa.CheckConstraint(
            "quantity_remaining >= 0", name=op.f("ck_credit_lots_quantity_remaining_not_negative")
        ),
        sa.CheckConstraint(
            "quantity_remaining <= quantity_granted",
            name=op.f("ck_credit_lots_remaining_lte_granted"),
        ),
        sa.CheckConstraint(f"source IN ({SOURCES})", name=op.f("ck_credit_lots_source_is_known")),
        # Redundant against the primary key on purpose: it exists so
        # `credit_transactions` can point a composite key at `(user_id, id)`.
        # `mentor_conferencing_options` carries the identical pair for the
        # identical reason.
        sa.UniqueConstraint("user_id", "id", name=op.f("uq_credit_lots_user_id_id")),
    )

    # One starter ever. **The predicate is the point** — on `user_id` alone this
    # would stop a user receiving a second monthly grant. Structural rather than
    # an application check, because onboarding's producer is a transition
    # somebody will eventually make idempotent by retrying it.
    op.create_index(
        "uq_credit_lots_one_starter_per_user",
        LOTS,
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("source = 'profile_completed'"),
    )
    # One opening balance per user, ever. The migration's loader runs more than
    # once by design — a rehearsal, a retry, the real cutover — and without this
    # every migrated user's balance silently doubles. PR 10's reconciliation
    # checks exactly this invariant; the index is what enforces it.
    op.create_index(
        "uq_credit_lots_one_opening_balance_per_user",
        LOTS,
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("source = 'opening_balance'"),
    )
    # The balance read: this user's lots, oldest expiry first, which is also the
    # order a spend consumes them in.
    op.create_index("ix_credit_lots_user_expiry", LOTS, ["user_id", "expires_at"])

    op.execute(
        f"CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON {LOTS} "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    op.create_table(
        TRANSACTIONS,
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("credit_lot_id", sa.Uuid(), nullable=False),
        # Signed. Negative spends, positive credits, zero refused.
        sa.Column("delta", sa.SmallInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        # Null on a grant and on an expiry sweep, which belong to no session.
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # **No `updated_at`.** Append-only — see the module docstring.
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credit_transactions")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_credit_transactions_user_id_users"),
            ondelete="RESTRICT",
        ),
        # **Composite, and that is the whole guard.** `user_id` is denormalised
        # from the lot so a ledger read is one table; the cost of the copy is
        # that the two can disagree, and nothing above the database would see
        # it — a lot-scoped balance and a transaction-scoped balance would each
        # look correctly filtered and return different numbers.
        #
        # A single-column key on `credit_lot_id` is satisfied by *any* lot row,
        # including another user's. `session_types.conferencing_option_id` met
        # this exact problem and is where the wording comes from.
        sa.ForeignKeyConstraint(
            ["user_id", "credit_lot_id"],
            ["credit_lots.user_id", "credit_lots.id"],
            name=op.f("fk_credit_transactions_lot_belongs_to_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name=op.f("fk_credit_transactions_session_id_sessions"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("delta <> 0", name=op.f("ck_credit_transactions_delta_is_not_zero")),
        sa.CheckConstraint(
            f"reason IN ({REASONS})", name=op.f("ck_credit_transactions_reason_is_known")
        ),
    )

    # One refund per session. The sweep commits per batch and is scheduled, so a
    # cancel followed by a sweep — or a sweep that runs twice — would otherwise
    # pay twice for one session.
    #
    # **The predicate is what makes it correct, not the column list.** A booking
    # debit and the refund reversing it share a `session_id`; keyed on
    # `session_id` alone this would reject the refund it exists to permit, and
    # every *rejecting* test would still pass.
    op.create_index(
        "uq_credit_transactions_one_refund_per_session",
        TRANSACTIONS,
        ["session_id"],
        unique=True,
        postgresql_where=sa.text(f"reason IN ({REFUND_REASONS})"),
    )
    op.create_index(
        "ix_credit_transactions_user_created",
        TRANSACTIONS,
        ["user_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    """Drop both tables, newest dependency first."""
    op.drop_index("ix_credit_transactions_user_created", table_name=TRANSACTIONS)
    op.drop_index("uq_credit_transactions_one_refund_per_session", table_name=TRANSACTIONS)
    op.drop_table(TRANSACTIONS)

    op.execute(f"DROP TRIGGER IF EXISTS trg_set_updated_at ON {LOTS}")
    op.drop_index("ix_credit_lots_user_expiry", table_name=LOTS)
    op.drop_index("uq_credit_lots_one_opening_balance_per_user", table_name=LOTS)
    op.drop_index("uq_credit_lots_one_starter_per_user", table_name=LOTS)
    op.drop_table(LOTS)
