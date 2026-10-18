"""When a profile counts as finished.

Pure, and in ``domain`` because it is the anti-farming boundary rather than a
query detail. What this predicate guards is a credit, and a credit is
spendable — so it deserves the scrutiny of an authorization rule, not a
condition buried in a ``WHERE`` clause nobody reads twice.

WHY COMPLETION AND NOT SIGNUP
=============================
The starter credit is granted when somebody finishes a profile, never when they
create an account. **Signing up is free; finishing a profile is work.** That
asymmetry is the whole anti-farming property, and it is the one the package's D9
was reaching for without having: D9 gated a flat ladder behind a referral, which
costs an attacker one throwaway address.

THE MENTEE BAR IS FOUR ANSWERS
==============================
A mentee finishes with at least one live help area, an origin country, a degree
goal and a field of study (Mentee Onboarding v5 B3, decision 261) — the answers
matching ranks on, so the credit pays for a profile a mentor can be matched to.
Before that the bar was row existence, which two nearly empty rows met. A
mentor's bar is unchanged: a profile and a mentor profile (PRD H7).

`missing_requirements` names what is still owed, in the order the flow asks
for it, so the refusal and `/me` tell the client which step to open.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, TypeGuard, get_args

__all__ = [
    "STEPS",
    "OnboardingRequirement",
    "OnboardingStep",
    "ProfileEvidence",
    "finishes_as_mentee",
    "may_complete_onboarding",
    "missing_codes",
    "missing_requirements",
    "published_step",
]
#: The onboarding steps a client may record as ``last_step`` (decision 260), in
#: flow order: the mentee's two, then the mentor's four (Mentor Onboarding B3).
#: The **only** list: mentor steps join it, never a second one. The
#: request schema types its field with it, so the check and the published enum
#: are one declaration.
OnboardingStep = Literal[
    "help", "plans", "mentor_help", "mentor_education", "mentor_about", "mentor_hours"
]
STEPS: tuple[str, ...] = get_args(OnboardingStep)


class OnboardingRequirement(StrEnum):
    """What a mentee must answer to finish onboarding, in the flow's order.

    The values are published: the `409` and `/me` name them, and the client
    opens the first one's step.
    """

    HELP = "help"
    ORIGIN = "origin"
    DEGREE = "degree"
    FIELD = "field"


@dataclass(frozen=True, slots=True)
class ProfileEvidence:
    """What the database can say about how far somebody got.

    Booleans rather than rows, so the rule below is testable without a database
    and cannot accidentally start reading a column it was not given.
    """

    has_profile: bool
    has_mentee_goal: bool
    has_mentor_profile: bool
    #: A goal need whose offering the platform still offers.
    has_live_need: bool
    has_origin: bool
    has_degree_goal: bool
    has_field_of_study: bool


def missing_requirements(evidence: ProfileEvidence) -> tuple[OnboardingRequirement, ...]:
    """The mentee requirements not yet met, in `OnboardingRequirement` order.

    Empty for a mentor: their bar has no mentee answers, so they are never
    sent to a mentee step.
    """
    if evidence.has_mentor_profile:
        return ()
    met = {
        OnboardingRequirement.HELP: evidence.has_live_need,
        OnboardingRequirement.ORIGIN: evidence.has_origin,
        OnboardingRequirement.DEGREE: evidence.has_degree_goal,
        OnboardingRequirement.FIELD: evidence.has_field_of_study,
    }
    return tuple(requirement for requirement in OnboardingRequirement if not met[requirement])


def missing_codes(evidence: ProfileEvidence) -> tuple[str, ...]:
    """`missing_requirements` as the published codes, the one form both
    refusals carrying `missing` send."""
    return tuple(item.value for item in missing_requirements(evidence))


def may_complete_onboarding(evidence: ProfileEvidence) -> bool:
    """Whether this user has done enough to finish onboarding.

    **A profile, and a finished role.** The profile is what a mentor reads
    before accepting; the role is a mentor profile, or a mentee goal with every
    requirement met. A predicate written with ``or`` where it meant ``and``
    still passes every positive case, which is why each missing requirement is
    enumerated in the tests rather than sampled.

    **Role-appropriate rather than mentee-only.** A mentor finishes onboarding
    too, and gating on the mentee answers would refuse the half of the platform
    that does not book — and a dual-role user who did the mentor half first.
    """
    mentee_done = evidence.has_mentee_goal and not missing_requirements(evidence)
    return evidence.has_profile and (mentee_done or evidence.has_mentor_profile)


def finishes_as_mentee(evidence: ProfileEvidence) -> bool:
    """Whether a completion is a mentee's, for the background bio draft
    (decision 269): a goal and no mentor profile. A mentor's bio is their public
    "about", which a mentee-voiced draft must never fill, so a user with both
    roles gets none.
    """
    return evidence.has_mentee_goal and not evidence.has_mentor_profile


def published_step(value: str | None) -> OnboardingStep | None:
    """A stored `last_step` as the flow names it, or `None`.

    Only `STEPS` are published: a value the ETL carried from Bubble (such as
    `"6"`) is not this flow's step, so it reads as never having started here,
    which is how a migrated mentee is told from one who came back mid-way.
    """
    return value if _is_step(value) else None


def _is_step(value: str | None) -> TypeGuard[OnboardingStep]:
    """`value` is one of `STEPS`: the list itself, never restated."""
    return value in STEPS
