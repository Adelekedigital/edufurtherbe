"""The M2 transform: what it maps, what it refuses, and what it reports.

Pure, so none of this needs a database — which is the point of keeping the
transform free of I/O. Every mapping decision in the migration is reviewable
here, at the speed of a unit test.

Several of these cover behaviour the dev export **cannot reach**. The
``unlisted_reason`` branch is the sharpest example: every unlisted mentor in the
extract is an orphan shell with no owner, so no attached mentor is unlisted and
the branch never executes against real data. It is also the one behaviour PR 42's
migration recorded as an obligation on this transform. A synthetic record is the
only thing that can hold it.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.domain.enums import ApprovalStatus, ListingStatus, MeetingProvider, UnlistedReason
from app.domain.transform import TransformError
from app.domain.transform.profiles import (
    SERVICE_OFFERINGS,
    export_date,
    plan_profiles,
    service_slugs,
    to_award,
    to_education,
    to_mentee_goal,
    to_mentor_profile,
)

NY = ZoneInfo("America/New_York")
THIS_YEAR = 2026

USER_ID = "1700000000000x000000000000000001"
THING_ID = "1700000000000x000000000000000099"


def record(**fields: Any) -> dict[str, Any]:
    """A canonical record, in the shape both source adapters produce."""
    return {
        "bubble_id": THING_ID,
        "created_at": "Sep 3, 2025 5:31 am",
        "modified_at": "Sep 4, 2025 5:31 am",
        "Creator": USER_ID,
        **fields,
    }


def user(**fields: Any) -> dict[str, Any]:
    return {"bubble_id": USER_ID, "email": "mentor@example.com", **fields}


# --------------------------------------------------------------------------
# Vocabularies
# --------------------------------------------------------------------------


def test_every_legacy_service_value_maps_to_one_of_the_six_parents() -> None:
    """Sixteen strings, six rows. The collapse is the whole point.

    Bubble stored one option set as text at the moment of selection, so the
    mentee side carries parents and the mentor side carries parents, children and
    renames. At mixed depth ``Document Review`` and ``document preparation`` are
    different strings for one concept and the matching join returns nothing.
    """
    assert len(SERVICE_OFFERINGS) == 16 - 4  # four differ only by case, folded away
    assert set(SERVICE_OFFERINGS.values()) == {
        "test-preparation",
        "document-preparation",
        "school-selection",
        "program-selection",
        "scholarships-financial-aid",
        "interview-preparation",
    }


@pytest.mark.parametrize(
    ("legacy", "slug"),
    [
        ("Document Review", "document-preparation"),
        ("Statement of Purpose", "document-preparation"),
        ("Letter of Recommendation", "document-preparation"),
        ("Application Interviews", "interview-preparation"),
        ("Visa Interview", "interview-preparation"),
        ("scholarships & funding guidance", "scholarships-financial-aid"),
        ("Scholarships & Financial Aid", "scholarships-financial-aid"),
    ],
)
def test_children_and_renames_collapse_onto_their_parent(legacy: str, slug: str) -> None:
    assert service_slugs(legacy, field="f", bubble_id="x") == (slug,)


def test_an_unmapped_service_raises_rather_than_being_dropped() -> None:
    """A migration that silently maps an unrecognised value to nothing produces
    plausible rows and no error, and only somebody who already suspects can find
    it."""
    with pytest.raises(TransformError, match="unmapped value"):
        service_slugs("Portfolio Review", field="f", bubble_id="x")


def test_repeated_values_are_deduplicated() -> None:
    """One mentor lists ``Document Review`` three times, and five legacy values
    collapse onto two parents — so duplicates come from the mapping itself, not
    only from the data.

    ``ON CONFLICT DO NOTHING`` absorbs them either way. What dedupe protects is
    the **reported count**, and a reconciliation whose numbers are not true is
    worse than none.
    """
    assert service_slugs(
        "Document Review , Document Review , Document Review", field="f", bubble_id="x"
    ) == ("document-preparation",)
    assert service_slugs("Document Review , Statement of Purpose", field="f", bubble_id="x") == (
        "document-preparation",
    )


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rendered", "expected"),
    [
        ("Jan 1, 2024 12:00 am", date(2024, 1, 1)),
        ("Dec 31, 2023 11:30 pm", date(2023, 12, 31)),
        ("Jul 4, 2024 8:00 pm", date(2024, 7, 4)),
    ],
)
def test_a_date_is_read_in_the_zone_bubble_wrote_it_in(rendered: str, expected: date) -> None:
    """The second case is the one that matters, and the export cannot produce it.

    ``parse_timestamp`` normalises to UTC, so taking ``.date()`` off the instant
    moves any evening value forward a day: ``Dec 31 11:30 pm`` in New York is
    ``2024-01-01 04:30Z``. **Every date in the dev export is exactly 12:00 am**,
    whose UTC date happens to agree — so the whole snapshot passes against the
    broken implementation. This was written wrong first and caught by probing a
    value the data cannot make.
    """
    assert export_date({"d": rendered}, "d", zone=NY, bubble_id="x") == expected


# --------------------------------------------------------------------------
# mentor_profiles — the branch the export cannot reach
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("approved", "available", "expected"),
    [
        ("Yes", "no", UnlistedReason.MENTOR_PAUSED),
        ("", "no", UnlistedReason.NEVER_APPROVED),
    ],
)
def test_an_unlisted_mentor_gets_an_explicit_reason(
    approved: str, available: str, expected: UnlistedReason
) -> None:
    """The column defaults to ``never_approved``, which is wrong for a migrated
    mentor who was approved and then paused themselves.

    PR 42's migration records this as an obligation on the transform because the
    column cannot express it. **No attached mentor in the dev export is
    unlisted** — both unlisted rows are orphan shells — so nothing but a
    synthetic record can hold this.
    """
    row = to_mentor_profile(
        record(**{"✅approvedText": approved, "availableStatus": available}),
        USER_ID,
        export_timezone=NY,
    )
    assert row.listing_status is ListingStatus.UNLISTED
    assert row.unlisted_reason is expected


def test_a_listed_mentor_has_no_unlisted_reason() -> None:
    row = to_mentor_profile(
        record(**{"✅approvedText": "Yes", "availableStatus": "yes"}), USER_ID, export_timezone=NY
    )
    assert row.approval_status is ApprovalStatus.APPROVED
    assert row.unlisted_reason is None


@pytest.mark.parametrize(
    ("legacy", "venue"),
    [
        ("Edufurther Video (Recommended)", MeetingProvider.DAILY),
        ("External Video Tool", MeetingProvider.CUSTOM),
        ("", MeetingProvider.GOOGLE_MEET),
    ],
)
def test_the_venue_mapping(legacy: str, venue: MeetingProvider) -> None:
    row = to_mentor_profile(record(meetingVenueSelection=legacy), USER_ID, export_timezone=NY)
    assert row.default_meeting_venue is venue


def test_a_blank_confirmation_is_false() -> None:
    """Blank meant "never turned it on" — settled decision 57. Ten of fifteen
    mentors in the extract are blank, so this is the majority path."""
    row = to_mentor_profile(record(confirmationRequired=""), USER_ID, export_timezone=NY)
    assert row.requires_booking_confirmation is False


# --------------------------------------------------------------------------
# Awards
# --------------------------------------------------------------------------


def test_an_award_year_inside_the_check_is_kept() -> None:
    row = to_award(
        record(**{"Award-institution": "Oxford", "Award-title": "Chevening", "Award-year": "2026"}),
        USER_ID,
        this_year=THIS_YEAR,
        export_timezone=NY,
    )
    assert row.year == 2026
    assert row.year_rejected is None


@pytest.mark.parametrize("bad", ["2029", "1600", "sometime"])
def test_an_award_year_outside_the_check_is_nulled_and_reported(bad: str) -> None:
    """The award survives; only the year is dropped.

    The column permits a null year, so keeping the row costs nothing and loses
    nothing that matters — the institution and title still render and the source
    value is still in the snapshot. Refusing the row would discard a real
    credential over one bad field; refusing the run would let one typo block a
    cutover.
    """
    row = to_award(
        record(**{"Award-institution": "Oxford", "Award-title": "Chevening", "Award-year": bad}),
        USER_ID,
        this_year=THIS_YEAR,
        export_timezone=NY,
    )
    assert row.year is None
    assert row.year_rejected == bad
    assert row.title == "Chevening"


# --------------------------------------------------------------------------
# Education and goals
# --------------------------------------------------------------------------


def test_education_keeps_the_raw_school_name_and_leaves_the_link_null() -> None:
    """``school_name_raw`` is always kept and ``institution_id`` is matched in a
    separate pass — the pair that makes an incomplete registry a display concern
    rather than data loss (ADR 0008)."""
    row = to_education(
        record(schoolName="Univerity of Lagos", degreeCategory="Bachelors"),
        USER_ID,
        export_timezone=NY,
    )
    assert row.school_name_raw == "Univerity of Lagos"
    assert row.degree_level_slug == "bachelors"
    assert row.degree_category == "Bachelors"


def test_education_without_a_school_name_is_refused() -> None:
    with pytest.raises(TransformError, match="required"):
        to_education(record(schoolName=""), USER_ID, export_timezone=NY)


def test_an_unmappable_degree_goal_is_kept_raw_rather_than_dropped() -> None:
    """The one lenient branch, and the package specifies it: 720 production rows
    held "Masters", "masters", "MSc" and "Master's Degree" as free text."""
    row = to_mentee_goal(
        record(**{"degreeGoal(text)": "Something Postgraduate"}), USER_ID, export_timezone=NY
    )
    assert row.degree_goal_slug is None
    assert row.degree_goal_raw == "Something Postgraduate"


def test_a_mappable_degree_goal_resolves_and_keeps_no_raw() -> None:
    row = to_mentee_goal(
        record(**{"degreeGoal(text)": "MSc (Master of Science)"}), USER_ID, export_timezone=NY
    )
    assert row.degree_goal_slug == "masters"
    assert row.degree_goal_raw is None


# --------------------------------------------------------------------------
# Ownership
# --------------------------------------------------------------------------


def test_a_row_no_user_points_at_is_reported_not_attributed() -> None:
    """``plan_identity`` established this and the reasoning carries: the link
    exists in one direction, and a row nobody claims cannot be attributed by
    inference."""
    plan = plan_profiles(
        [user()],
        education_records=[record(schoolName="University of Lagos")],
        goal_records=[],
        service_records=[],
        mentor_records=[],
        award_records=[],
        export_timezone=NY,
        this_year=THIS_YEAR,
    )
    # Unattached, not refused. A record that would transform perfectly well is
    # still reported rather than attributed — which is the distinction, and a
    # fixture missing a required field would pass this for the wrong reason.
    assert plan.errors == ()
    assert plan.education == ()
    assert plan.unattached["education"] == (THING_ID,)


def test_a_creator_that_disagrees_with_the_link_is_reported_and_the_link_wins() -> None:
    """``Creator`` is a cross-check, not the join. Now that it carries a Bubble
    user id the comparison is exact, so a disagreement means something real."""
    plan = plan_profiles(
        [user(**{"📚Education": THING_ID})],
        education_records=[
            record(Creator="1700000000000x000000000000000002", schoolName="University of Lagos")
        ],
        goal_records=[],
        service_records=[],
        mentor_records=[],
        award_records=[],
        export_timezone=NY,
        this_year=THIS_YEAR,
    )
    assert len(plan.education) == 1
    assert plan.education[0].user_bubble_id == USER_ID
    assert plan.creator_mismatches and "created by" in plan.creator_mismatches[0]


def test_an_award_whose_creator_is_unknown_is_reported_not_loaded() -> None:
    """Awards have no user-side link in either direction, so ``Creator`` is the
    only path — and it is validated against the known users rather than trusted,
    so an unknown creator is reported here instead of raising a foreign-key error
    deep inside the loader."""
    plan = plan_profiles(
        [user()],
        education_records=[],
        goal_records=[],
        service_records=[],
        mentor_records=[],
        award_records=[
            record(
                Creator="1700000000000x000000000000000404",
                **{"Award-institution": "X", "Award-title": "Y"},
            )
        ],
        export_timezone=NY,
        this_year=THIS_YEAR,
    )
    assert plan.awards == ()
    assert plan.unattached["user_awards"] == (THING_ID,)


# --------------------------------------------------------------------------
# The abbreviation a user actually holds
# --------------------------------------------------------------------------

#: Every distinct `shortForm` in the dev export, with what it must become.
#: Written out rather than expressed as a rule, because the two folds this has to
#: make are not the same fold: `BSc` -> `B.Sc` inserts punctuation, and
#: `B.sc` -> `B.Sc` fixes case. A regex that does both is a regex nobody can
#: predict, and the values are a closed set of nine.
EXPORT_SHORT_FORMS = {
    "BSc": "B.Sc",
    "B.sc": "B.Sc",
    "B.Eng": "B.Eng",
    "LL.B": "LL.B",
    "M.Sc": "M.Sc",
    "MSc": "M.Sc",
    "M.Eng": "M.Eng",
    "Ph.D": "Ph.D",
    "HND": "HND",
}


@pytest.mark.parametrize(("raw", "expected"), sorted(EXPORT_SHORT_FORMS.items()))
def test_the_short_form_a_user_holds_is_carried_and_normalised(raw: str, expected: str) -> None:
    """Legacy ``shortForm`` is populated on 21 of 21 rows and was read by nothing.

    It had to land before cutover: afterwards the Bubble data is gone and the
    value cannot be re-derived. The folds are real — the export spells one
    bachelor's degree as both ``BSc`` and ``B.sc``, and one master's as both
    ``M.Sc`` and ``MSc``.
    """
    row = to_education(
        record(schoolName="Somewhere", degreeCategory="Bachelors", shortForm=raw),
        USER_ID,
        export_timezone=NY,
    )

    assert row.degree_abbreviation == expected


def test_an_unlisted_short_form_is_kept_verbatim_rather_than_dropped() -> None:
    """The menu is advisory, so an abbreviation nobody listed is still the user's.

    Folding it to the nearest known value would be inventing a credential;
    dropping it would lose one. The same lenient branch ``degree_goal_raw``
    already takes, for the same reason.
    """
    row = to_education(
        record(schoolName="Somewhere", degreeCategory="Masters", shortForm="M.Litt"),
        USER_ID,
        export_timezone=NY,
    )

    assert row.degree_abbreviation == "M.Litt"


def test_no_short_form_inherits_rather_than_guessing() -> None:
    """Null means *inherit* the level's generic name, which is D21's rule."""
    row = to_education(
        record(schoolName="Somewhere", degreeCategory="Masters", shortForm=""),
        USER_ID,
        export_timezone=NY,
    )

    assert row.degree_abbreviation is None
