"""`fits`: one exact interval checked against availability, busy times and notice.

What booking an offered time asks once the grid may have moved under it (#347).
Each rule `bookable` applies to a step is asserted here on its own, positive and
negative, since an offer that passes one rule it should fail would be booked.
"""

from datetime import UTC, date, datetime, time, timedelta

from app.domain.availability import UtcInterval, WeeklyWindow, fits

LAGOS = "Africa/Lagos"
#: A Monday. Lagos is UTC+1 all year, so 09:00-12:00 there is 08:00-11:00 UTC.
MONDAY = date(2026, 11, 2)
NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)

MORNINGS = [WeeklyWindow(day_of_week=1, start_time=time(9), end_time=time(12), timezone=LAGOS)]


def at(hour: int, minute: int = 0, length: int = 60) -> UtcInterval:
    start = datetime(MONDAY.year, MONDAY.month, MONDAY.day, hour, minute, tzinfo=UTC)
    return UtcInterval(start, start + timedelta(minutes=length))


def check(slot: UtcInterval, *, busy: list[UtcInterval] | None = None, notice: int = 0) -> bool:
    return fits(
        rules=MORNINGS,
        exceptions=[],
        busy=busy or [],
        slot=slot,
        min_notice_minutes=notice,
        now=NOW,
    )


def test_an_hour_off_the_45_minute_grid_still_fits() -> None:
    """09:00 local + 60 minutes: the grid at 45 minutes offers 09:00, 09:45 and
    10:30 local, never 10:00, yet 10:00-11:00 local is wholly inside the window."""
    assert check(at(9))


def test_running_past_the_window_does_not_fit() -> None:
    assert not check(at(10, 30))


def test_starting_before_the_window_does_not_fit() -> None:
    assert not check(at(7, 30))


def test_a_taken_interval_overlapping_it_does_not_fit() -> None:
    assert not check(at(9), busy=[at(9, 30, 30)])


def test_a_session_ending_as_it_starts_does_not_block_it() -> None:
    """Half-open, as everywhere: `[)` touching is not overlapping."""
    assert check(at(9), busy=[at(8, 0, 60)])


def test_inside_the_notice_it_does_not_fit() -> None:
    hours_ahead = int((at(9).start - NOW).total_seconds() // 60)
    assert check(at(9), notice=hours_ahead)
    assert not check(at(9), notice=hours_ahead + 1)
