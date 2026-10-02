"""`is_mentee`: the one rule for the mentee half of `/me`."""

from __future__ import annotations

import pytest

from app.domain.roles import is_mentee


@pytest.mark.parametrize(
    ("has_goal", "has_mentor_profile", "expected"),
    [
        (True, False, True),  # an onboarded mentee
        (False, False, True),  # a mentee who never set a goal can still book
        (True, True, True),  # dual role
        (False, True, False),  # a mentor only
    ],
)
def test_who_gets_the_mentee_half(
    *, has_goal: bool, has_mentor_profile: bool, expected: bool
) -> None:
    assert is_mentee(has_goal=has_goal, has_mentor_profile=has_mentor_profile) is expected
