"""The mentee booking limits as a pure rule (#342)."""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

from app.core.errors import (
    BookingLimitReachedError,
    BookingOverlapError,
    BookingWithMentorExistsError,
)
from app.domain.booking_limits import MAX_LIVE_MENTEE_SESSIONS, LiveBooking, limit_breached

AT = dt.datetime(2026, 11, 2, 10, tzinfo=dt.UTC)
MENTOR = uuid4()


def held(hours_from_at: float, mentor: object = None, minutes: int = 60) -> LiveBooking:
    return LiveBooking(
        mentor_id=mentor or uuid4(),  # type: ignore[arg-type]
        starts_at=AT + dt.timedelta(hours=hours_from_at),
        duration_minutes=minutes,
    )


def test_nothing_held_allows_the_booking() -> None:
    assert limit_breached([], mentor_id=MENTOR, starts_at=AT, duration_minutes=60) is None


def test_one_held_with_another_mentor_at_another_time_allows_the_booking() -> None:
    breach = limit_breached([held(5)], mentor_id=MENTOR, starts_at=AT, duration_minutes=60)

    assert breach is None


def test_an_overlap_is_refused() -> None:
    breach = limit_breached([held(0.5)], mentor_id=MENTOR, starts_at=AT, duration_minutes=60)

    assert isinstance(breach, BookingOverlapError)


def test_touching_sessions_do_not_overlap() -> None:
    """Half-open windows: one ending at 10:00 and one starting at 10:00 coexist."""
    assert limit_breached([held(-1)], mentor_id=MENTOR, starts_at=AT, duration_minutes=60) is None
    assert limit_breached([held(1)], mentor_id=MENTOR, starts_at=AT, duration_minutes=60) is None


def test_a_second_live_session_with_the_same_mentor_is_refused() -> None:
    breach = limit_breached(
        [held(5, mentor=MENTOR)], mentor_id=MENTOR, starts_at=AT, duration_minutes=60
    )

    assert isinstance(breach, BookingWithMentorExistsError)


def test_the_cap_refuses_the_booking_that_would_exceed_it() -> None:
    live = [held(5 * (n + 1)) for n in range(MAX_LIVE_MENTEE_SESSIONS)]

    breach = limit_breached(live, mentor_id=MENTOR, starts_at=AT, duration_minutes=60)

    assert isinstance(breach, BookingLimitReachedError)


def test_one_below_the_cap_is_allowed() -> None:
    live = [held(5 * (n + 1)) for n in range(MAX_LIVE_MENTEE_SESSIONS - 1)]

    assert limit_breached(live, mentor_id=MENTOR, starts_at=AT, duration_minutes=60) is None


def test_an_overlap_is_reported_before_the_mentor_and_the_cap() -> None:
    """The order the mentee is told: the most specific reason first."""
    live = [held(0, mentor=MENTOR), held(5)]

    breach = limit_breached(live, mentor_id=MENTOR, starts_at=AT, duration_minutes=60)

    assert isinstance(breach, BookingOverlapError)


def test_the_same_mentor_is_reported_before_the_cap() -> None:
    live = [held(5, mentor=MENTOR), held(10)]

    breach = limit_breached(live, mentor_id=MENTOR, starts_at=AT, duration_minutes=60)

    assert isinstance(breach, BookingWithMentorExistsError)
