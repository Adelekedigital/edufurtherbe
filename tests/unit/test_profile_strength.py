"""The profile-completeness rule, as a pure function over profile facts (#223)."""

from __future__ import annotations

import dataclasses

import pytest

from app.domain.profile_strength import (
    BOOKABILITY,
    COMPLETENESS_ORDER,
    ProfileFacts,
    completeness,
)

#: A profile with every step done.
FULL = ProfileFacts(
    has_session_type=True,
    has_weekly_hours=True,
    photo_url="https://img.example/a.jpg",
    headline="I help with PhD applications",
    about="Ten years in admissions.",
    topic_count=1,
    has_origin_country=True,
    has_study_country=True,
    language_count=1,
    education_count=1,
    award_count=1,
)

#: A profile with nothing done.
BARE = ProfileFacts(
    has_session_type=False,
    has_weekly_hours=False,
    photo_url=None,
    headline=None,
    about=None,
    topic_count=0,
    has_origin_country=False,
    has_study_country=False,
    language_count=0,
    education_count=0,
    award_count=0,
)


def test_the_order_leads_with_what_stops_bookings() -> None:
    assert COMPLETENESS_ORDER[:2] == ("session_type", "weekly_hours")
    assert set(COMPLETENESS_ORDER[:2]) == BOOKABILITY
    assert len(COMPLETENESS_ORDER) == len(set(COMPLETENESS_ORDER)) == 9


def test_a_full_profile_and_a_bare_one() -> None:
    assert completeness(FULL).percent == 100
    assert completeness(FULL).missing == ()
    assert completeness(BARE).percent == 0
    assert completeness(BARE).missing == COMPLETENESS_ORDER
    assert completeness(BARE).setup_needed == ["session_type", "weekly_hours"]


def test_missing_keeps_the_priority_order() -> None:
    facts = dataclasses.replace(FULL, award_count=0, photo_url=None, has_weekly_hours=False)

    assert completeness(facts).missing == ("weekly_hours", "photo", "award")
    assert completeness(facts).setup_needed == ["weekly_hours"]


@pytest.mark.parametrize(("done_count", "percent"), [(0, 0), (1, 11), (5, 56), (8, 89), (9, 100)])
def test_percent_is_the_share_done_rounded(done_count: int, percent: int) -> None:
    """Undo steps from the end of the order until `done_count` remain."""
    undo = {
        "award": {"award_count": 0},
        "education": {"education_count": 0},
        "background": {"language_count": 0},
        "topics": {"topic_count": 0},
        "about": {"about": None},
        "headline": {"headline": None},
        "photo": {"photo_url": None},
        "weekly_hours": {"has_weekly_hours": False},
        "session_type": {"has_session_type": False},
    }
    changes: dict[str, object] = {}
    for code in list(undo)[: 9 - done_count]:
        changes |= undo[code]

    assert completeness(dataclasses.replace(FULL, **changes)).percent == percent  # type: ignore[arg-type]


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_blank_text_is_not_done(blank: str) -> None:
    """Writes store a blank as null, but migrated text may still be blank."""
    facts = dataclasses.replace(FULL, headline=blank, about=blank, photo_url=blank)

    assert set(completeness(facts).missing) == {"photo", "headline", "about"}


@pytest.mark.parametrize(
    "gap", [{"has_origin_country": False}, {"has_study_country": False}, {"language_count": 0}]
)
def test_background_needs_both_countries_and_a_language(gap: dict[str, object]) -> None:
    assert completeness(dataclasses.replace(FULL, **gap)).missing == ("background",)  # type: ignore[arg-type]
