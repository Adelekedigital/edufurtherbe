"""What a mentor application must hold before it is submitted (Mentor Onboarding B2)."""

from __future__ import annotations

from dataclasses import fields, replace
from decimal import Decimal

import pytest

from app.domain.mentor_application import (
    FROZEN_FIELDS,
    ApplicationEvidence,
    FrozenRecord,
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
    "avatar_focus": (Decimal("0.500"), Decimal("0.250")),
    "cover_color": "sky",
    "cover_art": "none",
    "origin_country_id": "ng",
    "guide_country_ids": ["us", "ca"],
    "languages": [{"language_id": "en", "proficiency": "fluent", "is_primary": True}],
}

#: The stored crop as a write sends it.
FOCUS = {"x": 0.5, "y": 0.25}


def test_resending_every_stored_value_changes_nothing() -> None:
    sent = {key: value for key, value in STORED.items() if key in FROZEN_FIELDS}
    sent |= {"about_me_drafted": False, "avatar_focus": FOCUS}
    assert frozen_changes(sent, STORED) == ()


def test_fields_outside_the_freeze_never_count() -> None:
    assert frozen_changes({"timezone": "UTC", "primary_role": "mentee"}, STORED) == ()


@pytest.mark.parametrize(
    ("sent", "named"),
    [
        ({"social_linkedin": None}, ("social_linkedin",)),
        ({"avatar_focus": {"x": 0.5, "y": 0.3}}, ("avatar",)),
        ({"cover_color": "mint"}, ("cover_color",)),
        ({"cover_art": "icons"}, ("cover_art",)),
        ({"origin_country_id": None}, ("origin_country_id",)),
        ({"guide_country_ids": []}, ("guide_country_ids",)),
        (
            {"languages": [{"language_id": "en", "proficiency": "native", "is_primary": True}]},
            ("languages",),
        ),
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


def test_a_linkedin_edit_is_not_a_change_but_clearing_it_is() -> None:
    """Decision 273 as amended 2026-10-19: the link may be corrected in review,
    never removed, since the reviewer needs one."""
    assert frozen_changes({"social_linkedin": "https://www.linkedin.com/in/bo"}, STORED) == ()
    assert frozen_changes({"social_linkedin": None}, STORED) == ("social_linkedin",)
    assert frozen_changes({"social_linkedin": None}, STORED | {"social_linkedin": None}) == ()


def test_guide_countries_are_an_ordered_list_and_null_writes_nothing() -> None:
    assert frozen_changes({"guide_country_ids": ["us", "ca"]}, STORED) == ()
    assert frozen_changes({"guide_country_ids": ["ca", "us"]}, STORED) == ("guide_country_ids",)
    assert frozen_changes({"guide_country_ids": None}, STORED) == ()


def test_languages_compare_as_a_set_of_their_stored_rows() -> None:
    english = {"language_id": "en", "proficiency": "fluent", "is_primary": True}
    french = {"language_id": "fr", "proficiency": "basic", "is_primary": False}
    assert frozen_changes({"languages": [english]}, STORED) == ()
    assert frozen_changes({"languages": [english, french]}, STORED) == ("languages",)
    assert frozen_changes({"languages": []}, STORED) == ("languages",)
    two = STORED | {"languages": [english, french]}
    assert frozen_changes({"languages": [french, english]}, two) == ()


def test_the_crop_is_compared_at_its_stored_precision() -> None:
    """A crop the client read back and resent, at a finer float, is the same crop."""
    assert frozen_changes({"avatar_focus": {"x": 0.5004, "y": 0.2496}}, STORED) == ()
    assert frozen_changes({"avatar_focus": {"x": 0.5005, "y": 0.25}}, STORED) == ("avatar",)
    no_crop = STORED | {"avatar_focus": (None, None)}
    assert frozen_changes({"avatar_focus": FOCUS}, no_crop) == ("avatar",)


def test_the_records_every_write_changes_are_named_as_published() -> None:
    """The `fields` an always-a-change write is refused under; the crop is the photo's."""
    assert [record.value for record in FrozenRecord] == ["avatar", "banner", "education", "awards"]
    assert FROZEN_FIELDS["avatar_focus"] == FrozenRecord.AVATAR
