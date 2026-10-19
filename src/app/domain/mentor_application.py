"""When a mentor application is complete enough to submit.

Pure, like `onboarding.missing_requirements`: the store gathers booleans, this
names what is still owed, and the `409` and the client read the same codes.

**Complete means bookable at approval.** Approval lists a mentor at once
(Mentor Onboarding PRD, Q1), so an application must already hold what a mentee
needs to book: weekly hours and a live session type, beside the profile a
reviewer reads. About is optional.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "EDUCATION",
    "FROZEN_FIELDS",
    "ApplicationEvidence",
    "MentorApplicationItem",
    "frozen_changes",
    "missing_items",
]


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


# --------------------------------------------------------------------------
# What a submitted application freezes (owner, 2026-10-18)
# --------------------------------------------------------------------------

#: The name an education write is refused under: every education write changes it.
EDUCATION = "education"

#: Request field -> the field a refusal names. The reviewer judges these, so they
#: hold still from submit to decision. The study country and programme come
#: from the education step, so they are education's.
FROZEN_FIELDS: dict[str, str] = {
    "social_linkedin": "social_linkedin",
    "about_me": "about_me",
    "about_me_drafted": "about_me",
    "about_me_prompt_version": "about_me",
    "headline": "headline",
    "offering_ids": "offering_ids",
    "primary_study_country_id": "primary_study_country_id",
    "primary_study_program": "primary_study_program",
}


def _same(field: str, sent: Any, stored: Mapping[str, Any]) -> bool:
    if field == "offering_ids":
        # `null` writes nothing, and the order is not the reviewer's concern.
        return sent is None or set(sent) == set(stored["offering_ids"])
    if field == "about_me_drafted":
        # `true` stamps a new draft time whatever is stored, so it always
        # changes the provenance; `false` changes it only when one is stored.
        return not sent and stored["about_me_drafted_at"] is None
    return bool(sent == stored[field])


def frozen_changes(sent: Mapping[str, Any], stored: Mapping[str, Any]) -> tuple[str, ...]:
    """The frozen fields `sent` would change, once each, in `FROZEN_FIELDS` order.

    Only a change counts: a form resending a reviewed field unchanged beside
    another edit saves. `sent` is already in its stored form (a LinkedIn link
    canonical, text trimmed), and so is `stored`.
    """
    named: dict[str, None] = {}
    for field, name in FROZEN_FIELDS.items():
        if field in sent and not _same(field, sent[field], stored):
            named[name] = None
    return tuple(named)
