"""Invite codes, and when an invite counts.

Both rules are pure and both guard a *recurring* grant, which is what makes them
worth this much scrutiny: the starter is one credit once, and an unlock opens
three a month indefinitely.
"""

from datetime import timedelta

import pytest

from app.domain.referrals import (
    CODE_LENGTH,
    HANDLE_MAX_LENGTH,
    HANDLE_MIN_LENGTH,
    MAX_CODE_LENGTH,
    RESERVED_HANDLES,
    RETIRED_CODE_HOLD,
    handle_problem,
    is_plausible_code,
    make_code,
    may_qualify,
    normalise_handle,
)


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


class TestHandles:
    """A picked invite handle (owner, 2026-10-15): one rule, here."""

    @pytest.mark.parametrize(
        "handle", ["ada", "ada-lovelace", "a1-b2-c3", "x" * HANDLE_MAX_LENGTH, "007"]
    )
    def test_a_well_formed_handle_is_accepted(self, handle: str) -> None:
        assert handle_problem(handle) is None

    @pytest.mark.parametrize(
        "handle",
        [
            "ab",
            "x" * (HANDLE_MAX_LENGTH + 1),
            "-ada",
            "ada-",
            "ada--lovelace",
            "ada lovelace",
            "ada_lovelace",
            "adé",
            "ada.lovelace",
            "",
        ],
    )
    def test_a_malformed_handle_is_refused(self, handle: str) -> None:
        assert handle_problem(handle) == "malformed"

    def test_the_bounds_are_three_and_thirty(self) -> None:
        """The edges of the permitted range, not the comfortable middle."""
        assert HANDLE_MIN_LENGTH == 3
        assert HANDLE_MAX_LENGTH == 30
        assert handle_problem("abc") is None
        assert handle_problem("ab") == "malformed"

    @pytest.mark.parametrize("handle", sorted(RESERVED_HANDLES))
    def test_a_reserved_word_is_refused(self, handle: str) -> None:
        """Too short to be a handle (``me``) is refused as malformed first;
        every other reserved word is refused as reserved."""
        expected = "malformed" if len(handle) < HANDLE_MIN_LENGTH else "reserved"
        assert handle_problem(handle) == expected

    def test_the_reserved_list_names_the_routes_and_the_brand(self) -> None:
        assert {
            "admin",
            "api",
            "invite",
            "edufurther",
            "support",
            "help",
            "login",
            "signup",
            "settings",
            "me",
            "null",
            "undefined",
        } <= RESERVED_HANDLES

    def test_input_is_lowercased_before_it_is_judged(self) -> None:
        assert normalise_handle("Ada-Lovelace") == "ada-lovelace"
        assert handle_problem(normalise_handle("ADMIN")) == "reserved"

    def test_the_hold_is_thirty_days(self) -> None:
        assert timedelta(days=30) == RETIRED_CODE_HOLD


class TestCodeShape:
    """What a code the public lookup will even try to resolve looks like."""

    def test_a_generated_code_is_plausible(self) -> None:
        assert is_plausible_code(make_code())

    def test_a_handle_is_plausible(self) -> None:
        assert is_plausible_code("ada-lovelace")

    @pytest.mark.parametrize(
        "code", ["", "x" * (MAX_CODE_LENGTH + 1), "a b", "a/b", "a%2Fb", "a.b", "é"]
    )
    def test_anything_else_is_not(self, code: str) -> None:
        assert not is_plausible_code(code)

    def test_the_longest_code_is_plausible(self) -> None:
        assert is_plausible_code("x" * MAX_CODE_LENGTH)
