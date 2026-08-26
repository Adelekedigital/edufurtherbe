"""A request that was never fulfilled hands the credit back.

Booking debits, but nothing returned the credit when the mentor **declined**,
the mentee **withdrew**, or the request **expired** unanswered — and
``credit_reason`` had no member that could even record it. A mentee who booked
and was declined simply lost a credit, which reads as being charged for
nothing.

Found by code review of the consumption PR. The handoff put refunds in PR 7 of
M5b-ii, but that PR is about the *post-session* cases — a mentor cancelling a
confirmed session, and a mentor not turning up. These three are pre-session and
were nobody's scope.

**One new reason for three transitions.** ``session_cancelled_refund`` and
``session_no_show_refund`` are separate because their policy differs: a mentor
cancelling refunds where a mentee cancelling does not. These three do not
differ — a request that never became a session always returns the credit,
whoever ended it. *Which* of the three it was is already recorded exactly by
``session_events.reason_code``, and a second copy of that vocabulary here is
what non-negotiable #8 calls a defect.

**Both objects move, and missing the second would be the expensive mistake.**
The vocabulary ``CHECK`` has to admit the new value, *and*
``uq_credit_transactions_one_refund_per_session`` has to count it as a refund.
The index is partial on the refund reasons: leave it alone and the new reason
sits outside it, so the scheduled expiry sweep — which re-runs, and whose own
docstring calls itself idempotent — would refund the same request on every pass.

Additive. No row carries the new value yet, so neither object can fail to
rebuild.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2a71d64c9e8"
down_revision: str | Sequence[str] | None = "c6b03f81a495"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "credit_transactions"
CHECK = "ck_credit_transactions_reason_is_known"
INDEX = "uq_credit_transactions_one_refund_per_session"

#: Written out rather than imported: **no migration imports from ``app``**.
#: `test_every_converted_enum_has_a_check_naming_its_values` compares these
#: literals against `CreditReason`, which is what keeps the two honest.
REASONS_BEFORE = (
    "'grant', 'session_booked', 'session_cancelled_refund', 'session_no_show_refund', 'lot_expired'"
)
REASONS_AFTER = (
    "'grant', 'session_booked', 'session_cancelled_refund', "
    "'session_no_show_refund', 'request_unfulfilled', 'lot_expired'"
)

REFUNDS_BEFORE = "'session_cancelled_refund', 'session_no_show_refund'"
REFUNDS_AFTER = "'session_cancelled_refund', 'session_no_show_refund', 'request_unfulfilled'"


def _swap_check(values: str) -> None:
    """A ``CHECK`` cannot be altered, so it is dropped and rebuilt.

    Cheap here and not in general: this is exactly what settled decision #100
    bought by moving these vocabularies off native enum types, where adding a
    value is a permanent ``ALTER TYPE ... ADD VALUE`` that cannot be undone.
    """
    # `op.f` on both: the naming convention is applied to a bare string on the
    # way in *and* on the way out, so an unwrapped name renders a second prefix
    # and the drop then looks for a constraint that was never created.
    op.drop_constraint(op.f(CHECK), TABLE, type_="check")
    op.create_check_constraint(op.f(CHECK), TABLE, f"reason IN ({values})")


def _swap_index(refunds: str) -> None:
    op.drop_index(INDEX, table_name=TABLE)
    op.create_index(
        INDEX,
        TABLE,
        ["session_id"],
        unique=True,
        postgresql_where=sa.text(f"reason IN ({refunds})"),
    )


#: Reasons that describe something that happened *to a session*, and therefore
#: cannot be recorded without naming one.
SESSION_BEARING = (
    "'session_booked', 'session_cancelled_refund', 'session_no_show_refund', 'request_unfulfilled'"
)

NEEDS_SESSION = "ck_credit_transactions_session_matches_reason"


def upgrade() -> None:
    """Admit the new reason, count it as a refund, and close the NULL hole."""
    _swap_check(REASONS_AFTER)
    _swap_index(REFUNDS_AFTER)

    # **The one-refund-per-session index is bypassed entirely by a NULL.**
    # PostgreSQL treats nulls as distinct, so two refund rows with no session
    # both insert and the guarantee the scheduled sweep leans on evaporates —
    # for any writer that forgets the column, which is exactly the writer this
    # schema refuses to trust everywhere else.
    #
    # The same hole lets a `session_booked` debit carry no session, which
    # defeats D8's first stated reason for the ledger: *"I was charged for a
    # session that never ran"* is unanswerable if the charge names nothing.
    #
    # Written as an equivalence so it cuts both ways — a grant or an expiry
    # sweep, which belong to no session, may not name one either.
    op.create_check_constraint(
        op.f(NEEDS_SESSION),
        TABLE,
        f"(reason IN ({SESSION_BEARING})) = (session_id IS NOT NULL)",
    )


def downgrade() -> None:
    """Narrow everything back. Any row carrying the new reason blocks this,
    which is the honest behaviour — the alternative is deleting somebody's
    refund."""
    op.drop_constraint(op.f(NEEDS_SESSION), TABLE, type_="check")
    _swap_index(REFUNDS_BEFORE)
    _swap_check(REASONS_BEFORE)
