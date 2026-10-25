"""Fields added to a response after the frontend shipped stay out of `required`.

The frontend's CI pulls the published spec on every run, and its fixtures build
these objects by hand. A field added as required turns its `main` red the moment
the spec publishes, before it can add the field: TypeScript rejects a property
the spec does not yet know. Optional, and always sent (FE, 2026-10-09).
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.main import create_app

#: Each schema, and the fields added to it after the frontend first consumed it.
ADDED = {
    "PartyRead": ("in_room_at", "degree", "institution", "cover_color", "cover_art"),
    # The cover on every card that shows an avatar (owner, 2026-10-20).
    "MentorSummaryRead": ("cover_color", "cover_art"),
    "SessionRead": ("mentee_attendance_sessions", "refund_until"),
    "UserRead": (
        "mentee_cancel_refund_hours",
        "onboarding",
        "languages",
        "links",
        # The effective session-type limit, mentors only (decision 285).
        "session_type_limit",
    ),
    "GoalRead": ("field_of_study", "schools"),
    # New with the goal's schools (Mentee Settings PRD): the link is optional.
    "GoalSchoolRead": ("institution_id", "institution_name"),
    "OnboardingStatusRead": ("last_step",),
    # Each offering's own next free time (decision 284).
    "SessionTypeRead": (
        "requires_booking_confirmation",
        "next_available_at",
        "next_available_state",
    ),
    "SessionAnswerRead": ("answered", "required"),
    # New with the mentor dashboard (decision 246): every figure optional from
    # the start. The window's bounds and the two blocks are required.
    "MentorStatsRead": ("window_days", "current_streak"),
    "MentorStatsWindowRead": (
        "completed_sessions",
        "mentoring_minutes",
        "mentees_mentored",
        "attendance_rate",
        "missed_by_mentor",
        "missed_by_mentee",
        "mentees_left_waiting",
        "cancelled_by_mentor",
        "review_count",
        "average_rating",
    ),
    # New with the mentee dashboard (decision 249), the same shape.
    "MenteeStatsRead": (
        "sessions_held",
        "mentors_met",
        "minutes_of_guidance",
        "missed_sessions",
        "missed_window_days",
        "window_days",
    ),
    "MenteeStatsWindowRead": ("completed_sessions", "minutes"),
    # New with the monthly chart (decision 247), on the same terms.
    "MentorMonthlyStatsRead": ("months",),
    "MentorMonthRead": (
        "completed_sessions",
        "missed_by_mentor",
        "cancelled_by_mentor",
        "cancelled_by_mentee",
        "cancelled_by_other",
    ),
    # New with `/me/mentors` (decision 248), as decision 246's models: the id,
    # the order and the state are required, everything else optional.
    "MetMentorRead": (
        "slug",
        "first_name",
        "last_name",
        "avatar_url",
        "avatar_focus",
        "degree",
        "institution",
        "session_count",
        "cover_color",
        "cover_art",
    ),
    # The mentor dashboard's review quote (decision 251).
    "OwnReviewRead": ("session_type",),
    # Reviews by service offering (decision 283).
    "MentorReviewRead": ("service_offerings",),
    "ReviewSummaryRead": ("review_topics",),
    # The invite landing (decision 256): `inviter` is required, its two
    # fields optional.
    "InviterRead": ("first_name", "avatar_url"),
    # Where the owner is from, on their own `/me` (Mentee Onboarding v5, #6).
    "UserProfileRead": ("origin_country", "about_me_drafted_at", "current_country"),
    # New with mentor matching (decision 265): the card, the reasons list, the
    # list and the verdict are required; every other field optional from the start.
    "MentorMatchRead": ("level", "study_country"),
    "MentorMatchesRead": ("for_mentor_bookable",),
    "MatchBasisRead": ("degree", "field", "country", "origin"),
    "DegreeBasisRead": ("id", "label", "raw"),
    # Where a mentor's bio came from, in their own view only (decision 267).
    "MentorPublicRead": ("about_me_drafted_at", "guide_countries", "links"),
    # A mentor application's state, in the owner's view (Mentor Onboarding B2).
    "MentorProfileRead": (
        "submitted_at",
        "approved_seen_at",
        "review_note",
        "reviewed_at",
        # The platform's response window (decision 278).
        "response_window_hours",
        # Where the mentor can guide mentees towards (decision 276), as on the public read.
        "guide_countries",
    ),
}


@pytest.mark.parametrize(("schema", "fields"), ADDED.items())
def test_an_added_field_is_not_required(schema: str, fields: tuple[str, ...]) -> None:
    spec = create_app(Settings(_env_file=None)).openapi()["components"]["schemas"][schema]  # type: ignore[call-arg]

    assert set(fields) <= set(spec["properties"])
    assert not set(fields) & set(spec.get("required", []))


#: Added fields the frontend's generator must type as optional. It types a
#: property with a spec `default` as always sent, so these publish none, though
#: the response always sends them (owner, 2026-10-20).
NO_DEFAULT = {
    "MentorSummaryRead": ("cover_art",),
    "PartyRead": ("cover_art",),
    "MetMentorRead": ("cover_art",),
}


@pytest.mark.parametrize(("schema", "fields"), NO_DEFAULT.items())
def test_an_added_field_publishes_no_default(schema: str, fields: tuple[str, ...]) -> None:
    spec = create_app(Settings(_env_file=None)).openapi()["components"]["schemas"][schema]  # type: ignore[call-arg]

    assert not [field for field in fields if "default" in spec["properties"][field]]
