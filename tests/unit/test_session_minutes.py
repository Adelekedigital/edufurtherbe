"""Measured minutes for an EduFurther video session (Integrations PRD, decision 4).

Measured is the time the mentor and the mentee were in the room together, from
each person's stays in Daily's records; one person waiting alone adds nothing.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.domain.session_minutes import co_present_minutes

T0 = dt.datetime(2026, 10, 21, 14, 0, tzinfo=dt.UTC)


def stay(start_min: float, end_min: float) -> tuple[dt.datetime, dt.datetime]:
    return (T0 + dt.timedelta(minutes=start_min), T0 + dt.timedelta(minutes=end_min))


@pytest.mark.parametrize("length", [30, 45, 60])
def test_both_in_the_room_the_whole_time_is_the_whole_length(length: int) -> None:
    assert co_present_minutes([stay(0, length)], [stay(0, length)]) == length


def test_only_the_time_together_counts() -> None:
    # Mentor 0-60, mentee arrives at 10 and leaves at 40: thirty together.
    assert co_present_minutes([stay(0, 60)], [stay(10, 40)]) == 30


def test_one_person_alone_adds_nothing() -> None:
    assert co_present_minutes([stay(0, 60)], []) == 0
    assert co_present_minutes([stay(0, 20)], [stay(30, 60)]) == 0


def test_a_rejoin_is_not_counted_twice() -> None:
    """A dropped connection is a second stay that may overlap the first, as
    Daily writes a record per join. Joined first, so nobody counts double."""
    mentor = [stay(0, 30), stay(25, 60)]
    mentee = [stay(0, 60), stay(0, 60)]
    assert co_present_minutes(mentor, mentee) == 60


def test_a_gap_while_one_side_reconnects_is_not_counted() -> None:
    mentor = [stay(0, 20), stay(25, 60)]
    assert co_present_minutes(mentor, [stay(0, 60)]) == 55


def test_an_overrun_counts_beyond_the_booking() -> None:
    assert co_present_minutes([stay(-2, 75)], [stay(0, 70)]) == 70


def test_partial_minutes_round_down() -> None:
    assert co_present_minutes([stay(0, 30.9)], [stay(0, 31)]) == 30
