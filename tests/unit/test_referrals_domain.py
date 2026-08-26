"""Invite codes, and when an invite counts.

Both rules are pure and both guard a *recurring* grant, which is what makes them
worth this much scrutiny: the starter is one credit once, and an unlock opens
three a month indefinitely.
"""

import pytest

from app.domain.referrals import CODE_LENGTH, make_code, may_qualify


class TestMakeCode:
    def test_a_code_is_long_enough_to_be_unguessable(self) -> None:
        """**The code is a bearer token in an email.**

        Anybody holding it can attach themselves to somebody else's invite, so
        its only protection is being infeasible to guess. Short codes are the
        version that works until somebody enumerates the space.
        """
        assert len(make_code()) >= CODE_LENGTH

    def test_two_codes_differ(self) -> None:
        """A generator returning a constant satisfies every length assertion."""
        assert make_code() != make_code()

    def test_codes_are_url_safe(self) -> None:
        """It travels in a link. `+` and `/` do not survive a query string
        unescaped, and an invite that breaks on paste is an invite nobody uses.
        """
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")

        assert set("".join(make_code() for _ in range(50))) <= allowed

    def test_the_space_is_large(self) -> None:
        """Fifty draws, no collision. Not proof, but it fails loudly against a
        generator with a tiny space — which is the mistake worth catching."""
        assert len({make_code() for _ in range(50)}) == 50


class TestMayQualify:
    def test_a_claimed_unqualified_referral_qualifies(self) -> None:
        assert may_qualify(has_invitee=True, already_qualified=False) is True

    def test_an_unclaimed_referral_does_not(self) -> None:
        """**Nobody arrived.** An invite that was sent and never answered has no
        invitee to have completed anything, and qualifying it would pay the
        referrer for sending an email."""
        assert may_qualify(has_invitee=False, already_qualified=False) is False

    def test_an_already_qualified_referral_does_not_requalify(self) -> None:
        """Onboarding completion is idempotent, so this predicate is asked more
        than once for the same referral. Re-qualifying would move `qualified_at`
        and — worse — invite a second grant."""
        assert may_qualify(has_invitee=True, already_qualified=True) is False

    @pytest.mark.parametrize(
        ("has_invitee", "already_qualified"),
        [(False, False), (False, True), (True, True)],
    )
    def test_every_refusing_combination(self, has_invitee: bool, already_qualified: bool) -> None:
        """Enumerated, because this gate opens a recurring benefit and a
        predicate written with `or` passes the single positive case."""
        assert may_qualify(has_invitee=has_invitee, already_qualified=already_qualified) is False
