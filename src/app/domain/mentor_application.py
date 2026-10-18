"""When a mentor application is complete enough to submit.

Pure, like `onboarding.missing_requirements`: the store gathers booleans, this
names what is still owed, and the `409` and the client read the same codes.

**Complete means bookable at approval.** Approval lists a mentor at once
(Mentor Onboarding PRD, Q1), so an application must already hold what a mentee
needs to book: weekly hours and a live session type, beside the profile a
reviewer reads. About is optional.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["ApplicationEvidence", "MentorApplicationItem", "missing_items"]


class MentorApplicationItem(StrEnum):
    """What a submitted application must hold, in the flow's step order.

    The values are published: the `409`'s `missing` names them, and the client
    opens the step of the first one.
    """

    OFFERINGS = "offerings"
    EDUCATION = "education"
    PRIMARY_STUDY_COUNTRY = "primary_study_country"
    AVATAR = "avatar"
    ORIGIN_COUNTRY = "origin_country"
    SOCIAL_LINKEDIN = "social_linkedin"
    HEADLINE = "headline"
    AVAILABILITY = "availability"
    SESSION_TYPE = "session_type"


@dataclass(frozen=True, slots=True)
class ApplicationEvidence:
    """What the database says the application holds, one boolean per item."""

    #: At least one service offering the platform still offers.
    has_offering: bool
    #: At least one live education entry with a degree, a field and both dates.
    has_education: bool
    has_primary_study_country: bool
    has_avatar: bool
    has_origin_country: bool
    has_linkedin: bool
    has_headline: bool
    #: Weekly hours the slot grid can use.
    has_availability: bool
    #: A live session type with a booking config.
    has_session_type: bool


_HELD = {
    MentorApplicationItem.OFFERINGS: "has_offering",
    MentorApplicationItem.EDUCATION: "has_education",
    MentorApplicationItem.PRIMARY_STUDY_COUNTRY: "has_primary_study_country",
    MentorApplicationItem.AVATAR: "has_avatar",
    MentorApplicationItem.ORIGIN_COUNTRY: "has_origin_country",
    MentorApplicationItem.SOCIAL_LINKEDIN: "has_linkedin",
    MentorApplicationItem.HEADLINE: "has_headline",
    MentorApplicationItem.AVAILABILITY: "has_availability",
    MentorApplicationItem.SESSION_TYPE: "has_session_type",
}


def missing_items(evidence: ApplicationEvidence) -> tuple[MentorApplicationItem, ...]:
    """Every item the application does not hold yet, in step order; empty when complete."""
    return tuple(item for item in MentorApplicationItem if not getattr(evidence, _HELD[item]))
