"""The focal point rule: the centre of the largest face, as fractions."""

from __future__ import annotations

from app.domain.avatar_focus import Face, focal_point


def test_the_centre_of_one_face_as_fractions() -> None:
    assert focal_point([Face(40, 20, 20, 40)], width=100, height=200) == (0.5, 0.2)


def test_the_largest_face_wins_over_a_smaller_one() -> None:
    """A mentor's photo is of the mentor; a poster or passer-by is smaller."""
    main = Face(10, 10, 60, 60)
    background = Face(80, 80, 10, 10)

    assert focal_point([background, main], width=100, height=100) == (0.4, 0.4)


def test_no_face_is_no_focus() -> None:
    assert focal_point([], width=100, height=100) is None


def test_a_box_past_the_edge_is_clamped_to_the_image() -> None:
    """A face partly out of frame can come back with a box beyond the edge."""
    assert focal_point([Face(90, -30, 40, 40)], width=100, height=100) == (1.0, 0.0)


def test_an_empty_image_is_no_focus() -> None:
    assert focal_point([Face(0, 0, 10, 10)], width=0, height=10) is None


def test_fractions_are_rounded_to_a_thousandth() -> None:
    x, y = focal_point([Face(0, 0, 1, 1)], width=3, height=7)  # type: ignore[misc]

    assert (x, y) == (0.167, 0.071)
