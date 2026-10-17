"""When a profile counts as finished, and why the bar is where it is.

**The rule is pure so the anti-farming decision is testable without a
database.** What it guards is a credit, and a credit is spendable — so the
predicate deserves the same scrutiny as an authorization check rather than
being buried in a query nobody reads twice.

A mentee finishes with a help area, an origin, a degree goal and a field of
study (Mentee Onboarding v5 B3, decision 261). A mentor's bar is unchanged.
"""

import pytest

from app.domain.onboarding import (
    OnboardingRequirement,
    ProfileEvidence,
    may_complete_onboarding,
    missing_requirements,
)

MENTEE_FACTS = ("has_live_need", "has_origin", "has_degree_goal", "has_field_of_study")


def evidence(**overrides: bool) -> ProfileEvidence:
    base = {
        "has_profile": True,
        "has_mentee_goal": True,
        "has_mentor_profile": False,
        **dict.fromkeys(MENTEE_FACTS, True),
    }
    return ProfileEvidence(**{**base, **overrides})


def bare_mentee_facts() -> dict[str, bool]:
    return dict.fromkeys(MENTEE_FACTS, False)


class TestMayCompleteOnboarding:
    def test_a_mentee_with_every_requirement_qualifies(self) -> None:
        assert may_complete_onboarding(evidence()) is True

    def test_a_mentor_with_a_profile_and_a_mentor_profile_qualifies(self) -> None:
        """**Role-appropriate, not mentee-only.** A mentor finishes onboarding
        too, and the mentee requirements are not theirs: mentor completion is
        unchanged (PRD H7)."""
        mentor = evidence(has_mentee_goal=False, has_mentor_profile=True, **bare_mentee_facts())
        assert may_complete_onboarding(mentor) is True

    def test_a_dual_role_user_qualifies_on_the_mentor_half(self) -> None:
        assert may_complete_onboarding(evidence(has_mentor_profile=True, has_origin=False)) is True

    def test_a_bare_account_does_not_qualify(self) -> None:
        """**The whole point of the gate.** Signing up is free and finishing a
        profile is work; a user with neither half has done nothing a script
        could not do."""
        bare = evidence(has_profile=False, has_mentee_goal=False, **bare_mentee_facts())
        assert may_complete_onboarding(bare) is False

    def test_a_goal_without_a_profile_does_not_qualify(self) -> None:
        assert may_complete_onboarding(evidence(has_profile=False)) is False

    def test_a_mentor_profile_without_a_profile_does_not_qualify(self) -> None:
        assert (
            may_complete_onboarding(evidence(has_profile=False, has_mentor_profile=True)) is False
        )

    @pytest.mark.parametrize("fact", ["has_mentee_goal", *MENTEE_FACTS])
    def test_a_mentee_missing_any_one_requirement_is_refused(self, fact: str) -> None:
        """Enumerated rather than sampled: this is the abuse boundary, and a
        predicate written with `or` where it meant `and` passes the positive
        tests above."""
        assert may_complete_onboarding(evidence(**{fact: False})) is False


class TestMissingRequirements:
    def test_a_complete_mentee_is_missing_nothing(self) -> None:
        assert missing_requirements(evidence()) == ()

    @pytest.mark.parametrize(
        ("fact", "code"),
        [
            ("has_live_need", OnboardingRequirement.HELP),
            ("has_origin", OnboardingRequirement.ORIGIN),
            ("has_degree_goal", OnboardingRequirement.DEGREE),
            ("has_field_of_study", OnboardingRequirement.FIELD),
        ],
    )
    def test_each_fact_names_its_code(self, fact: str, code: OnboardingRequirement) -> None:
        assert missing_requirements(evidence(**{fact: False})) == (code,)

    def test_everything_missing_comes_in_step_order(self) -> None:
        """The client opens the first missing step, so the order is the flow's."""
        assert missing_requirements(evidence(**bare_mentee_facts())) == (
            OnboardingRequirement.HELP,
            OnboardingRequirement.ORIGIN,
            OnboardingRequirement.DEGREE,
            OnboardingRequirement.FIELD,
        )

    def test_the_codes_are_the_published_strings(self) -> None:
        assert [code.value for code in OnboardingRequirement] == [
            "help",
            "origin",
            "degree",
            "field",
        ]
