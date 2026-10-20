"""Every mentor-profile write field is either a booking rule or profile.

`BOOKING_RULE_FIELDS` is what a declined mentor may not change (decision 279).
A field added to the write without being placed on one side fails here, rather
than becoming changeable by a declined mentor with every gate green.
"""

from __future__ import annotations

from app.api.schemas.profile import MentorProfileWrite
from app.domain.mentor_application import BOOKING_RULE_FIELDS

#: The rest of the write: the profile a declined mentor keeps editing.
PROFILE_FIELDS = frozenset(
    {
        "headline",
        "years_of_experience",
        "primary_study_country_id",
        "primary_study_program",
        "offering_ids",
        "guide_country_ids",
    }
)


def test_each_write_field_is_a_booking_rule_or_profile() -> None:
    fields = set(MentorProfileWrite.model_fields)

    assert fields == BOOKING_RULE_FIELDS | PROFILE_FIELDS
    assert not BOOKING_RULE_FIELDS & PROFILE_FIELDS
