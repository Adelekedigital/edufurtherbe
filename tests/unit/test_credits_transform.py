"""What a legacy ``bookingCredit`` becomes, and the two values that look alike.

**The test this file exists for is `''` against `'0'`.** Bubble stores the field
as text and those are different facts — never entered the credit system, versus
entered it and spent down — and `int(raw or 0)` collapses them while looking
correct. Measured finding 5 says five dev users hold the first and two the
second, so the collapse denies four people a credit and nothing downstream can
tell.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.domain.credits import STARTER_GRANT
from app.domain.transform.credits import (
    CREDIT_FIELD,
    PLAUSIBLE_CEILING,
    RENEW_DATE_FIELD,
    plan_opening_balances,
)

#: Mid-month, so end-of-month is unambiguous and nothing rides on the boundary.
CUTOVER = dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.UTC)
EXPIRY = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)


def a_record(anchor: str, credit: object, **extra: object) -> dict[str, object]:
    return {"unique id": anchor, CREDIT_FIELD: credit, **extra}


def plan(*records: dict[str, object], finished: frozenset[str] = frozenset()):
    return plan_opening_balances(list(records), cutover=CUTOVER, finished=finished)


# --------------------------------------------------------------------------
# The distinction the whole module exists for
# --------------------------------------------------------------------------


def test_empty_and_zero_are_different_facts() -> None:
    """**The one that `int(raw or 0)` gets wrong.**

    Both would become zero, both would be skipped, and the four dev users who
    never entered the credit system would silently lose the starter they are
    owed. Asserted together in one test because the bug is the *pair* being
    collapsed — two separate tests would both pass against a transform that
    treated them identically as "no lot".
    """
    result = plan(
        a_record("never", ""),
        a_record("spent", "0"),
        finished=frozenset({"never", "spent"}),
    )

    assert [row.user_bubble_id for row in result.starters] == ["never"]
    assert result.spent_down == ("spent",)
    assert result.lots == ()


def test_an_absent_field_is_the_same_as_empty() -> None:
    """Both mean Bubble never wrote a number. It is `'0'` that must stay
    distinct, and it does because it is not blank."""
    result = plan({"unique id": "absent"}, finished=frozenset({"absent"}))

    assert [row.user_bubble_id for row in result.starters] == ["absent"]


def test_an_unfinished_user_gets_nothing_yet() -> None:
    """They earn the starter the ordinary way when they finish. Granting it now
    would pay for work not yet done — and the partial unique index means the
    grant they later earn would be refused."""
    result = plan(a_record("halfway", ""), finished=frozenset())

    assert result.starters == ()
    assert result.unfinished == ("halfway",)
    assert result.lots == ()


# --------------------------------------------------------------------------
# The balances that do migrate
# --------------------------------------------------------------------------


def test_a_balance_becomes_a_lot_expiring_end_of_the_cutover_month() -> None:
    result = plan(a_record("holder", "5"))

    assert len(result.lots) == 1
    lot = result.lots[0]
    assert (lot.user_bubble_id, lot.quantity, lot.expires_at) == ("holder", 5, EXPIRY)


def test_every_lot_shares_one_expiry() -> None:
    """**Uniform, and that is the divergence from the runbook.**

    `03_MIGRATION_RUNBOOK.md` says `expires_at = bookingCreditRenewDate (or end
    of current month)`. The dates land on thirteen different days with one user
    in thirty-seven on the 1st, so carrying them would give each person a
    personal reset day the card contradicts on screen.
    """
    result = plan(
        a_record("a", "5", **{RENEW_DATE_FIELD: "Jun 30, 2025 11:17 am"}),
        a_record("b", "1", **{RENEW_DATE_FIELD: "Mar 3, 2026 8:00 am"}),
        a_record("c", "2"),
    )

    assert {row.expires_at for row in result.lots} == {EXPIRY}


def test_the_renew_date_reaches_no_field_at_all() -> None:
    """The accepting half of the one above: not merely overridden, unread.

    A transform that parsed the date and then discarded it would pass the test
    above and still raise on a malformed one at cutover.
    """
    result = plan(a_record("weird", "3", **{RENEW_DATE_FIELD: "not a date at all"}))

    assert result.lots[0].expires_at == EXPIRY
    assert result.quarantined == ()


def test_the_legacy_total_sums_only_the_lots() -> None:
    """**Starters are not a migrated balance.** Folding them in would make the
    reconciliation agree only if the legacy sum were wrong by exactly the number
    of starters."""
    result = plan(
        a_record("a", "5"),
        a_record("b", "2"),
        a_record("c", ""),
        finished=frozenset({"c"}),
    )

    assert result.legacy_credit_total == 7
    assert len(result.starters) == 1
    assert STARTER_GRANT == 1


# --------------------------------------------------------------------------
# What is refused, and reported rather than guessed
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["five", "3.5", "1,000", "-"])
def test_an_unreadable_value_is_quarantined(raw: str) -> None:
    """Somebody's balance that cannot be read. Coercing it either invents credits
    or destroys them, and both are invisible once loaded."""
    result = plan(a_record("odd", raw))

    assert result.lots == ()
    assert len(result.quarantined) == 1
    assert raw in result.quarantined[0].reason


def test_a_negative_balance_is_quarantined() -> None:
    """`ck_credit_lots_quantity_granted_positive` would refuse the row anyway;
    saying so here names the field rather than the constraint."""
    result = plan(a_record("owing", "-2"))

    assert result.lots == ()
    assert "negative" in result.quarantined[0].reason


def test_a_value_above_the_ceiling_is_quarantined_and_the_ceiling_itself_is_not() -> None:
    """The tripwire, and its accepting edge.

    Asserted as a pair because an off-by-one here is invisible: a transform
    written `>=` would quarantine a legitimate balance exactly at the ceiling,
    and the rejecting test alone would still pass.
    """
    result = plan(
        a_record("absurd", str(PLAUSIBLE_CEILING + 1)),
        a_record("at-the-line", str(PLAUSIBLE_CEILING)),
    )

    assert [row.user_bubble_id for row in result.lots] == ["at-the-line"]
    assert [row.user_bubble_id for row in result.quarantined] == ["absurd"]


def test_a_row_with_no_anchor_is_quarantined() -> None:
    result = plan({CREDIT_FIELD: "5"})

    assert result.lots == ()
    assert "no unique id" in result.quarantined[0].reason


def test_a_repeated_anchor_is_quarantined() -> None:
    """The partial unique index would collapse the second into nothing, and the
    reconciliation keys on the same anchor — so it would agree that one lot was
    expected and one landed, for two source rows."""
    result = plan(a_record("twice", "5"), a_record("twice", "2"))

    assert len(result.lots) == 1
    assert result.lots[0].quantity == 5
    assert "repeats" in result.quarantined[0].reason


# --------------------------------------------------------------------------
# The accounting identity the reconciliation rests on
# --------------------------------------------------------------------------


def test_every_source_row_lands_in_exactly_one_bucket() -> None:
    """**The identity `reconcile_credits` checks.** A source row that reaches no
    decision is a user nobody looked at, and the report would not mention them.
    """
    result = plan(
        a_record("holder", "5"),
        a_record("spent", "0"),
        a_record("never", ""),
        a_record("halfway", ""),
        a_record("odd", "five"),
        finished=frozenset({"never"}),
    )

    accounted = (
        {row.user_bubble_id for row in result.lots}
        | {row.user_bubble_id for row in result.starters}
        | set(result.spent_down)
        | set(result.unfinished)
        | {row.user_bubble_id for row in result.quarantined}
    )

    assert accounted == set(result.source_anchors)
    assert result.source_rows == 5


def test_the_report_names_every_quarantine() -> None:
    """A count is not actionable — the next step is always to go and look at the
    source row, and only the anchor gets you there."""
    result = plan(a_record("odd", "five"))

    assert "QUARANTINED odd" in result.report()
