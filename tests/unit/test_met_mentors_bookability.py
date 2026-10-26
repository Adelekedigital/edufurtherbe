"""One state per met mentor, by precedence: has_upcoming > pending > unavailable
> open (decision 248). Every combination of the three facts, written out, so
each rung is shown both winning and losing to the one above it."""

from __future__ import annotations

import pytest

from app.domain.met_mentors import Bookability, bookability, unavailable_reason


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


@pytest.mark.parametrize(
    ("state", "next_available", "reason"),
    [
        (Bookability.UNAVAILABLE, "none", "not_taking"),
        (Bookability.UNAVAILABLE, "open", "not_taking"),
        (Bookability.OPEN, "none", "no_open_times"),
        (Bookability.OPEN, "open", None),
        (Bookability.OPEN, "refreshing", None),
        (Bookability.HAS_UPCOMING, "none", None),
        (Bookability.PENDING, "none", None),
    ],
)
def test_why_a_mentor_cannot_be_booked(
    state: Bookability, next_available: str, reason: str | None
) -> None:
    """`not_taking` is exactly `unavailable`; `no_open_times` only for an `open`
    mentor whose card says nothing is free; an unknown time is not "none"."""
    assert unavailable_reason(state, next_available=next_available) == reason
