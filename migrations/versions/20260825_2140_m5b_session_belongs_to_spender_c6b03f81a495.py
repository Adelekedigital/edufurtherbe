"""A ledger movement cannot name somebody else's session.

Closes the MEDIUM `security-checker` raised against PR 1, at the point the
writer that could exploit it lands.

**The defect.** ``credit_transactions.user_id`` and ``session_id`` were
independent, so a row could debit one user while naming a session belonging to
another. Nothing above the database would see it: a per-user ledger and a
per-session ledger would each look correctly filtered and disagree, and a global
reconciliation of "lot sum equals ledger sum" passes either way.

**The fix is the shape PR 1 already used** for ``credit_lot_id`` — a composite
foreign key, with the redundant ``UNIQUE`` that makes it referenceable. Same
reasoning as ``session_types.conferencing_option_id``: *a single-column key is
satisfied by any row, including another user's.*

``MATCH SIMPLE`` is what makes this safe for grants and expiries, which carry a
null ``session_id``: when any column of a composite key is null the constraint
is not checked, so a grant is unaffected while every session-bearing row is
verified.

**Free now, expensive later.** No ``credit_transactions`` row carries a
``session_id`` yet — the writer that produces them ships in this same PR — so
this is two DDL statements rather than a backfill and a validate against real
rows.

Additive: ``UNIQUE (mentee_id, id)`` cannot fail on existing sessions, because
``id`` is already unique on its own.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c6b03f81a495"
# Re-pointed when PR 5 gained a migration below this one. Alembic's chain is
# linear, so a stacked migration fixes its `down_revision` in merge order.
down_revision: str | Sequence[str] | None = "d5e91c37b204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Make the composite referenceable, then point a key at it."""
    # Redundant against `pk_sessions` and deliberately so — its only job is to
    # give the foreign key below something to reference. Non-negotiable #10's
    # second sentence.
    op.create_unique_constraint("uq_sessions_mentee_id_id", "sessions", ["mentee_id", "id"])

    op.create_foreign_key(
        "fk_credit_transactions_session_belongs_to_user",
        "credit_transactions",
        "sessions",
        ["user_id", "session_id"],
        ["mentee_id", "id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_credit_transactions_session_belongs_to_user",
        "credit_transactions",
        type_="foreignkey",
    )
    op.drop_constraint("uq_sessions_mentee_id_id", "sessions", type_="unique")
