"""When a profile counts as finished, and why the bar is where it is.

**The rule is pure so the anti-farming decision is testable without a
database.** What it guards is a credit, and a credit is spendable — so the
predicate deserves the same scrutiny as an authorization check rather than
being buried in a query nobody reads twice.
"""

import pytest

from app.domain.onboarding import ProfileEvidence, may_complete_onboarding


def evidence(**overrides: bool) -> ProfileEvidence:
    base = {"has_profile": True, "has_mentee_goal": True, "has_mentor_profile": False}
    return ProfileEvidence(**{**base, **overrides})


class TestMayCompleteOnboarding:
    def test_a_mentee_with_a_profile_and_a_goal_qualifies(self) -> None:
        assert may_complete_onboarding(evidence()) is True

    def test_a_mentor_with_a_profile_and_a_mentor_profile_qualifies(self) -> None:
        """**Role-appropriate, not mentee-only.** A mentor finishes onboarding
        too, and gating the starter on a mentee goal would hand the credit only
        to the half of the platform that spends it — which reads sensible until
        a dual-role user is refused for having done the mentor half first."""
        assert (
            may_complete_onboarding(evidence(has_mentee_goal=False, has_mentor_profile=True))
            is True
        )

    def test_a_dual_role_user_qualifies(self) -> None:
        assert may_complete_onboarding(evidence(has_mentor_profile=True)) is True

    def test_a_bare_account_does_not_qualify(self) -> None:
        """**The whole point of the gate.**

        The starter is granted on finishing a profile rather than on signing up
        precisely because signing up is free and finishing a profile is work.
        A user with neither half has done nothing a script could not do.
        """
        assert may_complete_onboarding(evidence(has_profile=False, has_mentee_goal=False)) is False

    def test_a_profile_row_alone_does_not_qualify(self) -> None:
        assert may_complete_onboarding(evidence(has_mentee_goal=False)) is False

    def test_a_goal_without_a_profile_does_not_qualify(self) -> None:
        """Both halves, not either. A goal is one form; the profile is the
        thing a mentor reads before accepting."""
        assert may_complete_onboarding(evidence(has_profile=False)) is False

    @pytest.mark.parametrize(
        "case",
        [
            {"has_profile": False, "has_mentee_goal": False, "has_mentor_profile": False},
            {"has_profile": True, "has_mentee_goal": False, "has_mentor_profile": False},
            {"has_profile": False, "has_mentee_goal": True, "has_mentor_profile": False},
            {"has_profile": False, "has_mentee_goal": False, "has_mentor_profile": True},
        ],
    )
    def test_every_incomplete_combination_is_refused(self, case: dict[str, bool]) -> None:
        """Enumerated rather than sampled: this is the abuse boundary, and a
        predicate written with `or` where it meant `and` passes the three
        positive tests above."""
        assert may_complete_onboarding(ProfileEvidence(**case)) is False
