"""What a mentor application must hold before it is submitted (Mentor Onboarding B2)."""

from __future__ import annotations

from dataclasses import fields, replace

import pytest

from app.domain.mentor_application import (
    ApplicationEvidence,
    MentorApplicationItem,
    missing_items,
)

COMPLETE = ApplicationEvidence(**{field.name: True for field in fields(ApplicationEvidence)})
NOTHING = ApplicationEvidence(**{field.name: False for field in fields(ApplicationEvidence)})


def test_a_complete_application_misses_nothing() -> None:
    assert missing_items(COMPLETE) == ()


@pytest.mark.parametrize(
    ("held", "item"),
    [
        ("has_offering", "offerings"),
        ("has_education", "education"),
        ("has_primary_study_country", "primary_study_country"),
        ("has_avatar", "avatar"),
        ("has_origin_country", "origin_country"),
        ("has_linkedin", "social_linkedin"),
        ("has_headline", "headline"),
        ("has_availability", "availability"),
        ("has_session_type", "session_type"),
    ],
)
def test_each_item_is_named_alone_when_it_is_the_only_gap(held: str, item: str) -> None:
    assert missing_items(replace(COMPLETE, **{held: False})) == (MentorApplicationItem(item),)


def test_an_empty_application_names_every_item_in_step_order() -> None:
    """Help areas, education, about, hours: the client opens the first one's step."""
    assert [item.value for item in missing_items(NOTHING)] == [
        "offerings",
        "education",
        "primary_study_country",
        "avatar",
        "origin_country",
        "social_linkedin",
        "headline",
        "availability",
        "session_type",
    ]
