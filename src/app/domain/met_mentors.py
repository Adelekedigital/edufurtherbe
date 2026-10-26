"""Whether a mentee can book a mentor they have met again (decision 248).

**One state, chosen by precedence.** A mentor can be several of these at once: a
mentee may hold a confirmed session *and* a request awaiting the same mentor,
and either can be true of a mentor who has since stopped taking bookings. The
card shows one call to action, so the most specific fact wins:

    has_upcoming > pending > unavailable > open

A session already agreed is what the mentee should be pointed at, then a request
still waiting on the mentor; only with neither does it matter whether the mentor
can be booked now.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

__all__ = ["Bookability", "UnavailableReason", "bookability", "unavailable_reason"]


class Bookability(StrEnum):
    """What a mentee can do next with a mentor they have met."""

    #: A confirmed session with this mentor has not ended yet (in progress counts).
    HAS_UPCOMING = "has_upcoming"
    #: A request to this mentor is still waiting on their answer.
    PENDING = "pending"
    #: The mentor is not taking bookings: not live, or not set up to be booked.
    UNAVAILABLE = "unavailable"
    #: Bookable now, with nothing already in hand.
    OPEN = "open"


def bookability(*, has_upcoming: bool, pending: bool, taking_bookings: bool) -> Bookability:
    """The one state a card shows, by the precedence above."""
    if has_upcoming:
        return Bookability.HAS_UPCOMING
    if pending:
        return Bookability.PENDING
    if not taking_bookings:
        return Bookability.UNAVAILABLE
    return Bookability.OPEN


#: Why a mentor cannot be booked right now, for a card's copy (Mentee Home).
UnavailableReason = Literal["not_taking", "no_open_times"]


def unavailable_reason(state: Bookability, *, next_available: str) -> UnavailableReason | None:
    """``not_taking`` exactly when ``state`` is `UNAVAILABLE`; ``no_open_times``
    for a mentor who is `OPEN` but whose card says nothing is free in their
    window (``next_available`` is the profile's `none`); otherwise none.

    **Not a new `Bookability` member**: `open` keeps meaning "bookable" (decision
    248), so a client that does not read this field behaves as before. An
    unknown time (`refreshing`) is not "no open times": the job has not looked.
    """
    if state is Bookability.UNAVAILABLE:
        return "not_taking"
    if state is Bookability.OPEN and next_available == "none":
        return "no_open_times"
    return None
