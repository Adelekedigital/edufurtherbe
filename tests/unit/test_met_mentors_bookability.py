"""One state per met mentor, by precedence: has_upcoming > pending > unavailable
> open (decision 248). Every combination of the three facts, written out, so
each rung is shown both winning and losing to the one above it."""

from __future__ import annotations

import pytest

from app.domain.met_mentors import bookability


@pytest.mark.parametrize(
    ("has_upcoming", "pending", "taking_bookings", "state"),
    [
        (True, True, True, "has_upcoming"),
        (True, True, False, "has_upcoming"),
        (True, False, True, "has_upcoming"),
        (True, False, False, "has_upcoming"),
        (False, True, True, "pending"),
        (False, True, False, "pending"),
        (False, False, False, "unavailable"),
        (False, False, True, "open"),
    ],
)
def test_the_most_specific_fact_wins(
    has_upcoming: bool, pending: bool, taking_bookings: bool, state: str
) -> None:
    assert (
        bookability(has_upcoming=has_upcoming, pending=pending, taking_bookings=taking_bookings)
        == state
    )
