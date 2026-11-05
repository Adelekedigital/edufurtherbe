"""The four bands, and the number the card divides by, against a level.

Both are read-side rules with no column behind them, which is why they are
here rather than in a store: the balance is a `SUM`, and everything the card
renders is derived from it and the user's level allowance (decision 307).
"""

import pytest

from app.domain.credits import CreditState, allowance_for, state_for

#: The top level on the default ladder: a full month is three.
FULL = 3


class TestStateFor:
    @pytest.mark.parametrize(
        ("balance", "expected"),
        [
            (0, CreditState.EXHAUSTED),
            (1, CreditState.LOW),
            (2, CreditState.MODERATE),
            (3, CreditState.ON_TRACK),
            (4, CreditState.ON_TRACK),
        ],
    )
    def test_the_bands(self, balance: int, expected: CreditState) -> None:
        """Every boundary, because a band is defined by where it stops."""
        assert state_for(balance, FULL) == expected

    def test_a_balance_above_the_level_is_still_on_track(self) -> None:
        """**The open-ended top matters.** A refund or an admin grant can lift a
        balance past the level, and a band table written as `3..4` would leave
        five unclassified — which renders as an empty card rather than a full
        one."""
        assert state_for(6, FULL) == CreditState.ON_TRACK

    def test_a_full_level_two_month_is_on_track(self) -> None:
        """The top band is the user's level, not the ladder's top: a level-2
        mentee holding two is full."""
        assert state_for(2, 2) == CreditState.ON_TRACK
        assert state_for(2, 3) == CreditState.MODERATE

    def test_a_negative_balance_is_refused(self) -> None:
        """The database makes this unrepresentable; this is the second wall.

        Silently banding it as `exhausted` would let a real defect upstream —
        a spend that escaped `quantity_remaining >= 0` — render as an ordinary
        empty card and never be noticed.
        """
        with pytest.raises(ValueError, match="negative"):
            state_for(-1, FULL)


class TestAllowanceFor:
    def test_the_ordinary_case_is_the_level(self) -> None:
        for balance in (0, 1, 2, 3):
            assert allowance_for(balance, FULL) == FULL

    def test_the_bar_moves_when_a_credit_is_spent(self) -> None:
        """**A fixed denominator is what makes the bar move**: it holds still
        while the balance falls, so each spend empties one segment."""
        spending = [allowance_for(b, FULL) for b in (3, 2, 1, 0)]

        assert spending == [FULL] * 4

    def test_a_balance_above_the_level_raises_it(self) -> None:
        """It raises rather than clamps: a refund can lift a balance past the
        level, and clamping would read "4 credits left" beside a bar with three
        positions."""
        assert allowance_for(4, FULL) == 4
        assert allowance_for(7, FULL) == 7

    def test_the_boundary_is_not_off_by_one(self) -> None:
        assert allowance_for(FULL, FULL) == FULL

    def test_a_lower_level_draws_a_shorter_bar(self) -> None:
        assert allowance_for(0, 1) == 1
        assert allowance_for(1, 2) == 2
