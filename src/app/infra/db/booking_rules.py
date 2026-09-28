"""The booking window and break a session type actually uses (settled decision #200).

**One rule: the offering's own value, else its mentor's default, else the
platform's.** `COALESCE` over the config row and the mentor profile — the same
inherit pattern approval uses — written once here and read by every query that
needs it, so the slots a mentee sees, the booking that checks them and the card's
next free time cannot disagree about what an offering allows.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, literal

from app.domain.availability import DEFAULT_BREAK_MINUTES, MAX_PROJECTION_DAYS
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.sessions import SessionTypeBookingConfig

__all__ = ["effective_break_minutes", "effective_window_days"]


def effective_window_days(
    config: Any = SessionTypeBookingConfig, mentor: Any = MentorProfile
) -> Any:
    """Days ahead the offering may be booked. Pass aliases when a query joins twice."""
    return func.coalesce(
        config.booking_window_days, mentor.booking_window_days, literal(MAX_PROJECTION_DAYS)
    )


def effective_break_minutes(
    config: Any = SessionTypeBookingConfig, mentor: Any = MentorProfile
) -> Any:
    """Minutes of break after a session of the offering."""
    return func.coalesce(
        config.break_after_minutes, mentor.break_after_minutes, literal(DEFAULT_BREAK_MINUTES)
    )
