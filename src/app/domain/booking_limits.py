"""How much a mentee may have booked at once (#342, settled decision 231).

Three rules, decided by the owner on 2026-10-03, each counted over **live**
sessions — pending a mentor's answer, or confirmed and not yet settled:

1. no two live sessions whose times overlap, whoever the mentors are;
2. at most one live session with any one mentor;
3. at most :data:`MAX_LIVE_MENTEE_SESSIONS` live sessions in all.

Pure, so the rule is one function the booking path calls and a unit test can
read whole. The database reads and the lock that makes them race-safe live in
``infra/db/mentee_limits.py``; the overlap rule is also an ``EXCLUDE``
constraint, because a count can be raced and a constraint cannot.

**Checked in that order**, and the order is what the mentee is told. An overlap
is the most specific thing wrong with the request; a second session with the
same mentor is next; the overall cap is the most general, and is reported only
when nothing narrower explains the refusal.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from app.core.errors import (
    BookingLimitReachedError,
    BookingOverlapError,
    BookingWithMentorExistsError,
    ConflictError,
)

__all__ = ["MAX_LIVE_MENTEE_SESSIONS", "OVERLAP_MESSAGE", "LiveBooking", "limit_breached"]

#: The most live sessions a mentee may hold at once. A policy number, kept here
#: so changing it is one edit; #307 is where per-policy configuration moves.
MAX_LIVE_MENTEE_SESSIONS = 2

#: Shared with the booking writer, which raises it when the constraint catches
#: an overlap this check lost a race to.
OVERLAP_MESSAGE = "you already have a session at this time"


@dataclass(frozen=True)
class LiveBooking:
    """One of the mentee's live sessions, as far as these rules need it."""

    mentor_id: UUID
    starts_at: dt.datetime
    duration_minutes: int


def _overlaps(a_start: dt.datetime, a_minutes: int, b_start: dt.datetime, b_minutes: int) -> bool:
    """Half-open, ``[start, end)``, as ``session_window`` builds its range: two
    sessions that merely touch do not overlap."""
    a_end = a_start + dt.timedelta(minutes=a_minutes)
    b_end = b_start + dt.timedelta(minutes=b_minutes)
    return a_start < b_end and b_start < a_end


def limit_breached(
    live: Iterable[LiveBooking],
    *,
    mentor_id: UUID,
    starts_at: dt.datetime,
    duration_minutes: int,
) -> ConflictError | None:
    """The refusal a new booking earns, or ``None`` when it is allowed."""
    held = list(live)
    if any(_overlaps(s.starts_at, s.duration_minutes, starts_at, duration_minutes) for s in held):
        return BookingOverlapError(OVERLAP_MESSAGE)
    if any(s.mentor_id == mentor_id for s in held):
        return BookingWithMentorExistsError("you already have a session with this mentor")
    if len(held) >= MAX_LIVE_MENTEE_SESSIONS:
        return BookingLimitReachedError(
            f"you already have {MAX_LIVE_MENTEE_SESSIONS} sessions pending or coming up"
        )
    return None
