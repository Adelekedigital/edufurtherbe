"""The window, break, length and notice a session type actually uses (#204, #216).

**One rule: the offering's own value, else its mentor's default, else the
platform's.** `COALESCE` over the config row and the mentor profile — the same
inherit pattern approval uses — written once here and read by every query that
needs it, so the slots a mentee sees, the booking that checks them and the card's
next free time cannot disagree about what an offering allows.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, literal

from app.core.config import MIN_BOOKING_WINDOW_DAYS
from app.domain.availability import (
    DEFAULT_BREAK_MINUTES,
    DEFAULT_DURATION_MINUTES,
    DEFAULT_MIN_NOTICE_MINUTES,
    BookingWindow,
)
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.sessions import SessionTypeBookingConfig

__all__ = [
    "effective_break_minutes",
    "effective_duration_minutes",
    "effective_min_notice_minutes",
    "effective_requires_confirmation",
    "effective_window_days",
    "inherits_duration",
    "inherits_min_notice",
]


def effective_window_days(
    window: BookingWindow, config: Any = SessionTypeBookingConfig, mentor: Any = MentorProfile
) -> Any:
    """Days ahead the offering may be booked. Pass aliases when a query joins twice.

    **Clamped to the configured maximum on read**, never rewritten: lowering the
    maximum shortens every longer window at once, and raising it back restores
    what each mentor chose — no migration either way. **Floored at
    `MIN_BOOKING_WINDOW_DAYS`** the same way, so a window stored before that
    minimum existed can never hold nothing at the longest notice (#311).
    """
    return func.greatest(
        func.least(
            func.coalesce(
                config.booking_window_days,
                mentor.booking_window_days,
                literal(window.default_days),
            ),
            literal(window.max_days),
        ),
        literal(MIN_BOOKING_WINDOW_DAYS),
    )


def effective_break_minutes(
    config: Any = SessionTypeBookingConfig, mentor: Any = MentorProfile
) -> Any:
    """Minutes of break after a session of the offering."""
    return func.coalesce(
        config.break_after_minutes, mentor.break_after_minutes, literal(DEFAULT_BREAK_MINUTES)
    )


def effective_duration_minutes(
    config: Any = SessionTypeBookingConfig, mentor: Any = MentorProfile
) -> Any:
    """How long a session of the offering runs, and the step between its slots (#216)."""
    return func.coalesce(
        config.duration_minutes, mentor.default_duration_minutes, literal(DEFAULT_DURATION_MINUTES)
    )


def effective_min_notice_minutes(
    config: Any = SessionTypeBookingConfig, mentor: Any = MentorProfile
) -> Any:
    """How far ahead the offering must be booked (#216)."""
    return func.coalesce(
        config.min_notice_minutes,
        mentor.default_min_notice_minutes,
        literal(DEFAULT_MIN_NOTICE_MINUTES),
    )


def effective_requires_confirmation(
    config: Any = SessionTypeBookingConfig, mentor: Any = MentorProfile
) -> Any:
    """Whether booking the offering is a request the mentor must accept (#106).

    The offering's own setting, else its mentor's, which is `NOT NULL`, so there
    is no platform default to fall to. Booking obeys this, and the public read
    shows it, so a mentee is told before "Send request" what booking will do.
    """
    return func.coalesce(config.requires_booking_confirmation, mentor.requires_booking_confirmation)


def inherits_duration(config: Any = SessionTypeBookingConfig) -> Any:
    """Whether the offering's length comes from its mentor or the platform."""
    return config.duration_minutes.is_(None)


def inherits_min_notice(config: Any = SessionTypeBookingConfig) -> Any:
    """Whether the offering's notice comes from its mentor or the platform."""
    return config.min_notice_minutes.is_(None)
