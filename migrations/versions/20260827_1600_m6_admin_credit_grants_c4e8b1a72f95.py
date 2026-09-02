"""``admin_credit_grants``, and the sixth ``credit_source``.

Two changes, and only one of them is a table.

**``admin_grant`` joins the source vocabulary.** It was deferred at PR 1 under
settled decision #21 — *a vocabulary is not a wish list* — on the argument that
nothing wrote one. This migration ships with the endpoint that does, which is the
rule working rather than being set aside.

Settled decision #100 is what makes that cheap: closed vocabularies are ``text``
plus a ``CHECK``, never a PostgreSQL enum. Adding a value is dropping and
recreating a constraint; on a native enum it would be irreversible, which is the
whole reason #100 exists.

**``admin_credit_grants`` records who authorised one**, in a table beside the
ledger rather than as columns on it. `credit_transactions` records *movements*;
who authorised one is metadata about a single kind of movement, and two nullable
columns used by one source in six would be dead weight on every other row. The
same shape `review_reports` takes beside `reviews`.

The composite key is the authorization, as everywhere else here: on
``credit_lots.id`` alone this row could name user A while pointing at user B's
lot, and a per-user audit and a per-lot audit would each look correctly filtered
and disagree.

Additive. The new CHECK is strictly wider than the one it replaces, so no
existing row can fail it, and the table is new.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e8b1a72f95"
# Re-pointed when #218 merged. That stack and this one both branched from
# `a91e37c4d820`; the review moderation table landed first, so this one fixes its
# `down_revision` in merge order — Alembic's chain is linear and two heads make
# `alembic upgrade head` fail outright (how-we-work rule 1).
down_revision: str | Sequence[str] | None = "b8d4f26a91c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "admin_credit_grants"

#: Written out rather than imported: **no migration imports from ``app``**.
#: `test_every_converted_enum_has_a_check_naming_its_values` compares these
#: literals against the classes, which is what keeps the two honest.
SOURCES_BEFORE = (
    "'profile_completed', 'referral_unlock', 'monthly_free', 'refund', 'opening_balance'"
)
SOURCES_AFTER = (
    "'profile_completed', 'referral_unlock', 'monthly_free', 'refund', "
    "'opening_balance', 'admin_grant'"
)


def upgrade() -> None:
    """Widen the vocabulary, then add the table that writes into it."""
    op.drop_constraint(op.f("ck_credit_lots_source_is_known"), "credit_lots", type_="check")
    op.create_check_constraint(
        op.f("ck_credit_lots_source_is_known"),
        "credit_lots",
        f"source IN ({SOURCES_AFTER})",
    )

    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("credit_lot_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        # NOT NULL although `note` is not: a record that cannot say who is not a
        # record — `review_reports.resolved_by` states the same rule.
        sa.Column("granted_by", sa.Uuid(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_credit_grants")),
        # **Composite, and it is the authorization.** See the module docstring.
        sa.ForeignKeyConstraint(
            ["user_id", "credit_lot_id"],
            ["credit_lots.user_id", "credit_lots.id"],
            name=op.f("fk_admin_credit_grants_lot_belongs_to_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_admin_credit_grants_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["granted_by"],
            ["users.id"],
            name=op.f("fk_admin_credit_grants_granted_by_users"),
            ondelete="RESTRICT",
        ),
        # One authorisation per lot. Two would mean two admins each believing
        # they granted it, and the audit could not say which.
        sa.UniqueConstraint("credit_lot_id", name=op.f("uq_admin_credit_grants_credit_lot_id")),
    )

    # Every grant one admin made, newest first — the question an audit asks
    # *second*.
    op.create_index(
        "ix_admin_credit_grants_granted_by",
        TABLE,
        ["granted_by", sa.text("created_at DESC")],
    )
    # **The one it asks first.** The index above leads on `granted_by` and
    # omits `id`, so the unfiltered page — the endpoint's documented default —
    # has nothing to seek on and would sort the whole table every request.
    op.create_index(
        "ix_admin_credit_grants_created",
        TABLE,
        [sa.text("created_at DESC"), sa.text("id DESC")],
    )

    # **No `trg_set_updated_at`.** The table is append-only and carries no
    # `updated_at`; attaching the trigger would succeed and raise only at the
    # first UPDATE, which an append-only table never receives. ADR 0027 §4
    # records that trap and the test that catches it.


def downgrade() -> None:
    """Drop the table, then narrow the vocabulary back.

    **In that order, and it matters.** Narrowing first would fail against any
    `admin_grant` lot the table still explains — which is the correct failure,
    but an unhelpful one: the operator would be told a constraint is violated
    rather than that real grants are in the way.
    """
    op.drop_index("ix_admin_credit_grants_created", table_name=TABLE)
    op.drop_index("ix_admin_credit_grants_granted_by", table_name=TABLE)
    op.drop_table(TABLE)

    # This fails loudly if any `admin_grant` lot survives the table. That is
    # deliberate: the lots are somebody's credits, and silently rewriting their
    # source to make a downgrade pass is the kind of quiet data change the
    # ledger exists to prevent.
    op.drop_constraint(op.f("ck_credit_lots_source_is_known"), "credit_lots", type_="check")
    op.create_check_constraint(
        op.f("ck_credit_lots_source_is_known"),
        "credit_lots",
        f"source IN ({SOURCES_BEFORE})",
    )
