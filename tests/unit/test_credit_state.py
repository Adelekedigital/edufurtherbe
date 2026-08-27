"""The four bands, and the number the card divides by.

Both are read-side rules with no column behind them, which is why they are
here rather than in a store: the balance is a `SUM`, and everything the card
renders is derived from it.
"""

import pytest

from app.domain.credits import (
    CreditState,
    allowance_for,
    credit_ladder,
    state_for,
)


class TestStateFor:
    @pytest.mark.parametrize(
        ("balance", "expected"),
        [
            (0, CreditState.EXHAUSTED),
            (1, CreditState.LOW),
            (2, CreditState.MODERATE),
            (3, CreditState.MODERATE),
            (4, CreditState.ON_TRACK),
            (5, CreditState.ON_TRACK),
        ],
    )
    def test_the_bands(self, balance: int, expected: CreditState) -> None:
        """Every boundary, because a band is defined by where it stops."""
        assert state_for(balance) == expected

    def test_a_balance_above_five_is_still_on_track(self) -> None:
        """**The open-ended top matters.** A late refund can push a balance past
        the steady state of four, and a band table written as `4..5` would leave
        six unclassified — which renders as an empty card rather than a full
        one."""
        assert state_for(6) == CreditState.ON_TRACK

    def test_a_negative_balance_is_refused(self) -> None:
        """The database makes this unrepresentable; this is the second wall.

        Silently banding it as `exhausted` would let a real defect upstream —
        a spend that escaped `quantity_remaining >= 0` — render as an ordinary
        empty card and never be noticed.
        """
        with pytest.raises(ValueError, match="negative"):
            state_for(-1)


class TestAllowanceFor:
    def test_the_ordinary_case_is_the_steady_state_ceiling(self) -> None:
        for balance in (0, 1, 2, 3, 4):
            assert allowance_for(balance) == credit_ladder().steady_state

    def test_the_bar_moves_when_a_credit_is_spent(self) -> None:
        """**The defect this replaced, stated as a test.**

        Dividing by `credit_ladder().monthly` made `balance == allowance` for every
        balance at or above three: a mentee at the steady state of four saw a
        full four-segment bar, spent one, and saw a full *three*-segment bar.
        The number changed and the picture did not.

        A fixed denominator is the whole fix, so the assertion is that the
        denominator holds still while the balance falls.
        """
        spending = [allowance_for(b) for b in (4, 3, 2, 1, 0)]

        assert spending == [credit_ladder().steady_state] * 5

    def test_a_balance_above_the_ceiling_raises_it(self) -> None:
        """It raises rather than clamps: migrated users arrive at five, and a
        late refund can push a balance past four. Clamping would read "5 credits
        left" beside a bar with four positions."""
        assert allowance_for(5) == 5
        assert allowance_for(7) == 7

    def test_the_boundary_is_not_off_by_one(self) -> None:
        assert allowance_for(credit_ladder().steady_state) == credit_ladder().steady_state

    def test_the_ceiling_is_the_starter_plus_the_month(self) -> None:
        assert (
            credit_ladder().steady_state == credit_ladder().starter + credit_ladder().monthly == 4
        )
