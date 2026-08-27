"""The credit vocabularies and the two rules that decide when a lot dies.

**Every value here has a producer, and that is the assertion.** Settled decision
#21 struck `credit_source` by name — the canonical DDL declares seven sources
while payments are out of scope by decision #8, so `purchase` is a schema
asserting a choice nobody took. The test below fails when a member is added
without something in this phase that writes it.

The expiry rules are here rather than beside the grant call sites because there
are four of them — the starter, the unlock, the monthly job and the ETL — and a
predicate in four places is non-negotiable #8 rather than four call sites being
explicit.
"""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.core.config import get_settings
from app.domain.credits import (
    CreditReason,
    CreditSource,
    allowance_for,
    credit_ladder,
    end_of_month,
    expiry_for,
    refund_expiry,
)


class TestVocabularies:
    """A member with no producer is the defect #21 exists to catch."""

    def test_every_source_shipped_has_a_producer_in_this_phase(self) -> None:
        assert {source.value for source in CreditSource} == {
            "profile_completed",
            "referral_unlock",
            "monthly_free",
            "refund",
            "opening_balance",
        }

    def test_purchase_and_promotional_are_absent(self) -> None:
        """Payments are out of scope (#8) and nothing grants promotionally.

        Named rather than left to the set comparison above, because these two are
        exactly what the canonical DDL would have had us ship.
        """
        values = {source.value for source in CreditSource}
        assert "purchase" not in values
        assert "promotional" not in values
        assert "admin_grant" not in values

    def test_every_reason_shipped_has_a_producer_in_this_phase(self) -> None:
        assert {reason.value for reason in CreditReason} == {
            "grant",
            "session_booked",
            "session_cancelled_refund",
            "session_no_show_refund",
            # Three producers, one member: `transition()` on decline and on
            # withdraw, and `expire_requests()` on the sweep. They share a
            # member because they share a *policy* — a request that never
            # became a session always returns the credit — while the two above
            # are separate precisely because theirs differ.
            "request_unfulfilled",
            "lot_expired",
        }

    def test_no_show_forfeit_is_absent(self) -> None:
        """A mentee who does not turn up writes no row.

        The canonical DDL carries `session_no_show_forfeit`, which reads as a
        transaction and is not one: the credit left the balance when the session
        was booked, and not refunding it is the *absence* of a row rather than a
        negative one. A member for it would invite a double debit.
        """
        assert "session_no_show_forfeit" not in {reason.value for reason in CreditReason}


class TestQuantities:
    def test_the_earning_ladder(self) -> None:
        """Three independent quantities, asserted independently **on purpose**.

        They happen to satisfy ``starter + unlock == monthly`` today, and
        chaining them that way would assert a
        relationship the design does not claim: the unlock is a one-off on top
        of the starter, the allowance is what arrives every month, and nothing
        holds them equal. Raise the allowance to four and a chained assertion
        goes red for a reason that is not the bug.
        """
        ladder = credit_ladder()
        assert ladder.starter == 1
        assert ladder.unlock == 2
        assert ladder.monthly == 3

    def test_steady_state_is_four_not_three(self) -> None:
        """The non-expiring starter plus the monthly three.

        This is why the read publishes ``max(allowance, balance)`` rather than
        the allowance alone — the card would otherwise show "4 credits left"
        beside a bar that cannot draw four.
        """
        ladder = credit_ladder()
        assert ladder.starter + ladder.monthly == ladder.steady_state == 4


class TestEndOfMonth:
    @pytest.mark.parametrize(
        ("moment", "expected"),
        [
            (datetime(2026, 9, 1, 0, 0, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC)),
            (datetime(2026, 9, 30, 23, 59, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC)),
            (datetime(2026, 12, 15, 12, 0, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC)),
            # February, and a leap year, because the naive `day + 31` is wrong twice.
            (datetime(2028, 2, 3, 9, 0, tzinfo=UTC), datetime(2028, 3, 1, tzinfo=UTC)),
        ],
    )
    def test_it_is_the_first_of_the_next_month(self, moment: datetime, expected: datetime) -> None:
        """**Exclusive, and that is the card's promise.**

        The dashboard renders `Next reset date: 1st, Sep 2026`, so a lot granted
        in August must still be spendable on 31 August and dead on 1 September.
        An inclusive end-of-August would kill it a day early, which the card
        would then contradict on screen.
        """
        assert end_of_month(moment) == expected

    def test_it_rolls_the_year(self) -> None:
        assert end_of_month(datetime(2026, 12, 31, 23, 0, tzinfo=UTC)).year == 2027


class TestExpiryFor:
    def test_the_starter_never_expires(self) -> None:
        """It exists so a user gets a feel for the platform.

        Expiring it makes the reward depend on the day somebody happened to
        finish onboarding — complete on the 28th and it lives three days.
        """
        assert (
            expiry_for(CreditSource.PROFILE_COMPLETED, now=datetime(2026, 8, 28, tzinfo=UTC))
            is None
        )

    @pytest.mark.parametrize(
        "source",
        [CreditSource.REFERRAL_UNLOCK, CreditSource.MONTHLY_FREE, CreditSource.OPENING_BALANCE],
    )
    def test_every_other_grant_dies_at_the_reset(self, source: CreditSource) -> None:
        now = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
        assert expiry_for(source, now=now) == datetime(2026, 9, 1, tzinfo=UTC)

    def test_refund_is_not_answerable_here(self) -> None:
        """A refund inherits from the lot it replaces, so it needs that lot.

        Routing it through this function would mean guessing, and the guess that
        looks harmless — end of month — is the one that turns a purchased credit
        perishable.
        """
        with pytest.raises(ValueError, match="refund"):
            expiry_for(CreditSource.REFUND, now=datetime(2026, 8, 20, tzinfo=UTC))


class TestRefundExpiry:
    def test_a_refund_of_a_perishable_credit_dies_at_the_next_reset(self) -> None:
        """Not the original date — the original may already have passed.

        Both refund triggers are settled *after* the session, so a mentor
        no-show on 2 September refunds a credit spent in August. Returning the
        original date would hand back something already dead.
        """
        original = datetime(2026, 8, 31, 23, 0, tzinfo=UTC)
        assert refund_expiry(original, now=datetime(2026, 9, 2, tzinfo=UTC)) == datetime(
            2026, 10, 1, tzinfo=UTC
        )

    def test_a_refund_of_a_never_expiring_credit_never_expires(self) -> None:
        """The starter, today. Every purchased credit, once payments land.

        D7 is blunt about the second: expiring something somebody bought is a
        chargeback and, in several jurisdictions, unlawful. Inheriting the
        *semantics* rather than the date is what makes that safe before the
        payments work exists to test it.
        """
        assert refund_expiry(None, now=datetime(2026, 9, 2, tzinfo=UTC)) is None


class TestEndOfMonthNormalises:
    """**The reset is a platform boundary, so the caller's zone must not decide
    it.** The implementation read `moment.year`/`moment.month` from whatever
    tzinfo it was handed, while the module docstring promised UTC."""

    def test_a_lagos_instant_in_the_last_hour_of_the_month(self) -> None:
        """`2026-09-01T00:30+01:00` is `2026-08-31T23:30Z` — still August.

        Unnormalised this returned October: a whole extra month of credit for
        anyone east of UTC granted in the final hour.
        """
        from zoneinfo import ZoneInfo

        lagos = datetime(2026, 9, 1, 0, 30, tzinfo=ZoneInfo("Africa/Lagos"))

        assert end_of_month(lagos) == datetime(2026, 9, 1, tzinfo=UTC)

    def test_a_toronto_instant_in_the_first_hours_of_the_month(self) -> None:
        """The mirror case, west of UTC: `2026-08-31T21:00-04:00` is already
        `2026-09-01T01:00Z`, so the lot belongs to September's reset."""
        from zoneinfo import ZoneInfo

        toronto = datetime(2026, 8, 31, 21, 0, tzinfo=ZoneInfo("America/Toronto"))

        assert end_of_month(toronto) == datetime(2026, 10, 1, tzinfo=UTC)

    def test_a_naive_datetime_is_refused(self) -> None:
        """Assuming UTC is how an offset gets silently discarded — the same
        reason every timestamp column here is `timestamptz`."""
        with pytest.raises(ValueError, match="aware"):
            end_of_month(datetime(2026, 8, 15, 12, 0))


@pytest.fixture
def fresh_settings() -> Iterator[None]:
    """Clear the settings cache on the way in **and on the way out**.

    `get_settings` is `lru_cache(maxsize=1)`, so a test that overrides an
    environment variable and clears the cache leaves the overridden `Settings`
    in it. `monkeypatch` restores the variable at teardown; nothing restores the
    cache, so the next test to read the ladder gets the previous test's numbers
    and the suite starts passing or failing on execution order.

    Both sides, because either alone is a trap: without the entry clear the
    override is never seen, and without the exit clear it is never forgotten.

    The exit half was watched failing rather than assumed. Dropping it and
    running the whole class still passed — the last test in it raises
    `ValidationError`, and `lru_cache` does not cache exceptions, so the cache
    happened to be empty afterwards. Running only
    `test_an_override_moves_the_grant` and then `test_credit_state.py` is the
    ordering that exposes it: two allowance tests go red against a leaked
    monthly grant of five.
    """
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.usefixtures("fresh_settings")
class TestTheLadderIsConfigured:
    """The sizes come from the environment; the rules do not.

    **What is configurable and what is not is the point of this class.** How much
    a starter is worth can move; that it never expires cannot, because
    `NON_EXPIRING` is a statement about what the credit is *for* rather than
    about its size.
    """

    def test_the_defaults_are_what_shipped(self) -> None:
        """An unset environment behaves exactly as before this became
        configuration — which is what makes the change safe to deploy ahead of
        deciding any new numbers."""
        ladder = credit_ladder()

        assert (ladder.starter, ladder.unlock, ladder.monthly) == (1, 2, 3)

    def test_an_override_moves_the_grant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The whole reason for the change. `get_settings` is `lru_cache`d, so a
        test that sets the variable without clearing the cache reads the value
        from whichever test ran first — which is the failure this clears for."""
        monkeypatch.setenv("CREDIT_MONTHLY_ALLOWANCE", "5")
        monkeypatch.setenv("CREDIT_STARTER_GRANT", "2")
        get_settings.cache_clear()

        ladder = credit_ladder()

        assert (ladder.starter, ladder.monthly) == (2, 5)

    def test_the_steady_state_follows_the_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """**The relationship, not the number.** The card's denominator is the
        starter plus the monthly grant, so raising either has to move the bar —
        otherwise a mentee sits above a ceiling that never grew."""
        monkeypatch.setenv("CREDIT_MONTHLY_ALLOWANCE", "5")
        get_settings.cache_clear()

        assert credit_ladder().steady_state == 1 + 5
        assert allowance_for(4) == 6

    def test_a_grant_of_zero_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`ck_credit_lots_quantity_granted_positive` would refuse the lot at the
        database; refusing it at startup names the setting instead of the
        constraint, and fails before anybody is granted nothing."""
        monkeypatch.setenv("CREDIT_STARTER_GRANT", "0")
        get_settings.cache_clear()

        with pytest.raises(ValidationError):
            credit_ladder()

    def test_a_negative_grant_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A debit wearing a grant's name."""
        monkeypatch.setenv("CREDIT_MONTHLY_ALLOWANCE", "-3")
        get_settings.cache_clear()

        with pytest.raises(ValidationError):
            credit_ladder()
