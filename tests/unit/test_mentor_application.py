"""What a mentor application must hold before it is submitted (Mentor Onboarding B2)."""

from __future__ import annotations

from dataclasses import fields, replace

import pytest

from app.domain.mentor_application import (
    FROZEN_FIELDS,
    ApplicationEvidence,
    MentorApplicationItem,
    frozen_changes,
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


# --------------------------------------------------------------------------
# What a submitted application freezes
# --------------------------------------------------------------------------

STORED = {
    "social_linkedin": "https://www.linkedin.com/in/ada",
    "about_me": "Bio",
    "about_me_drafted_at": None,
    "about_me_prompt_version": None,
    "headline": "MSc",
    "offering_ids": ["a", "b"],
    "primary_study_country_id": "gb",
    "primary_study_program": None,
}


def test_resending_every_stored_value_changes_nothing() -> None:
    sent = {key: value for key, value in STORED.items() if key in FROZEN_FIELDS}
    assert frozen_changes(sent | {"about_me_drafted": False}, STORED) == ()


def test_fields_outside_the_freeze_never_count() -> None:
    assert frozen_changes({"timezone": "UTC", "primary_role": "mentee"}, STORED) == ()


@pytest.mark.parametrize(
    ("sent", "named"),
    [
        ({"social_linkedin": None}, ("social_linkedin",)),
        ({"about_me_drafted": True}, ("about_me",)),
        # a redraft re-stamps the draft time even over a stored one
        ({"about_me_drafted": True, "about_me": "Bio"}, ("about_me",)),
        ({"about_me_prompt_version": "bio_draft-v1"}, ("about_me",)),
        ({"offering_ids": ["a"]}, ("offering_ids",)),
        ({"primary_study_program": "Law"}, ("primary_study_program",)),
    ],
)
def test_each_change_names_its_field(sent: dict[str, object], named: tuple[str, ...]) -> None:
    assert frozen_changes(sent, STORED) == named


def test_help_areas_compare_as_a_set_and_null_writes_nothing() -> None:
    assert frozen_changes({"offering_ids": ["b", "a"]}, STORED) == ()
    assert frozen_changes({"offering_ids": None}, STORED) == ()


def test_about_me_is_named_once_however_many_of_its_fields_change() -> None:
    sent = {"about_me": "New", "about_me_drafted": True, "about_me_prompt_version": "x-v1"}
    assert frozen_changes(sent, STORED) == ("about_me",)


def test_a_redraft_over_a_stored_draft_still_changes_its_provenance() -> None:
    """The writer stamps a new draft time on every `true`, so the check must too."""
    drafted = STORED | {"about_me_drafted_at": "2026-10-18", "about_me_prompt_version": "v-1"}
    sent = {"about_me": "Bio", "about_me_drafted": True, "about_me_prompt_version": "v-1"}
    assert frozen_changes(sent, drafted) == ("about_me",)
