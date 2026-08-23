"""The scale, and the two windows — asserted without a database or a request.

Everything here is pure, which is the point of `domain/`: the same rules the ETL
and the request producer will use are testable with no I/O at all.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.domain.reviews import (
    MENTOR_RATINGS,
    ORDINAL_SCALE,
    REVIEW_EDIT_WINDOW,
    REVIEW_INTERVAL,
    MentorRating,
    edit_window_open,
    from_ordinal,
    to_ordinal,
)

WRITTEN_AT = dt.datetime(2026, 8, 22, 12, 0, tzinfo=dt.UTC)


def test_the_scale_is_pinned_to_its_ordinals() -> None:
    """**The guard for the one hazard `MentorRating` creates.**

    The ordinal is the member's position, so nothing declares the mapping twice —
    and so reordering the members silently changes what every stored row means.
    There is no migration to catch it and no type error: `1` is still an `int`.

    This is the test that turns a silent data corruption into a red gate, which
    is why it asserts the pairs rather than the round trip. A round trip passes
    against any consistent ordering, including a wrong one.
    """
    assert to_ordinal(MentorRating.NOT_GREAT) == 1
    assert to_ordinal(MentorRating.GREAT) == 2
    assert to_ordinal(MentorRating.EXCELLENT) == 3


def test_the_scale_has_exactly_the_points_the_column_allows() -> None:
    """The vocabulary and the `CHECK` bound are one declaration, seen twice."""
    low, high = ORDINAL_SCALE

    assert low == 1
    assert len(list(MentorRating)) == high


@pytest.mark.parametrize("rating", list(MentorRating))
def test_every_rating_survives_the_round_trip(rating: MentorRating) -> None:
    assert from_ordinal(to_ordinal(rating)) is rating


@pytest.mark.parametrize("value", [0, 4, -1])
def test_a_number_off_the_scale_is_refused_rather_than_guessed(value: int) -> None:
    """A row holding `4` is a database disagreeing with this module.

    Returning the nearest member would publish a guess as the mentee's answer,
    which is worse than failing: nobody would find out.
    """
    with pytest.raises(ValueError, match="point on the"):
        from_ordinal(value)


def test_the_edit_window_is_open_immediately_after_writing() -> None:
    assert edit_window_open(WRITTEN_AT, WRITTEN_AT)


def test_the_edit_window_is_open_just_inside_its_edge() -> None:
    assert edit_window_open(WRITTEN_AT, WRITTEN_AT + REVIEW_EDIT_WINDOW - dt.timedelta(seconds=1))


def test_the_edit_window_shuts_exactly_on_its_edge() -> None:
    """Half-open, matching `join_window` — the boundary belongs to one side only.

    A window that is open *at* its edge and shut a microsecond later is a rule
    two readers implement differently.
    """
    assert not edit_window_open(WRITTEN_AT, WRITTEN_AT + REVIEW_EDIT_WINDOW)


def test_the_two_windows_are_not_the_same_order_of_magnitude() -> None:
    """A guard against a copy-paste that would make an edit a re-review.

    They are different rules with different jobs — ten minutes to fix a typo,
    thirty days before the next review — and the failure mode if one is pasted
    over the other is silent: every window still "works", just wrongly.
    """
    assert REVIEW_EDIT_WINDOW < REVIEW_INTERVAL
    assert dt.timedelta(minutes=10) == REVIEW_EDIT_WINDOW
    assert dt.timedelta(days=30) == REVIEW_INTERVAL


def test_the_summary_publishes_every_rating_the_database_averages() -> None:
    """A fifth question would be averaged in SQL and dropped in silence.

    `MENTOR_RATINGS` drives the column list, the `CHECK` bounds and
    `profile_summary`, so adding one is a single edit everywhere except here —
    and pydantic ignores an unknown key rather than raising. `extra="forbid"`
    turns the drop into an error; this turns the *omission* into one.
    """
    from app.api.schemas.reviews import ReviewSummaryRead

    assert set(MENTOR_RATINGS) <= set(ReviewSummaryRead.model_fields), (
        "the summary is missing a rating the database averages"
    )
