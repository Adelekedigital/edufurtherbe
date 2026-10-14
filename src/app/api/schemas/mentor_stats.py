"""A mentor's own dashboard figures (decision 246).

**The window's bounds and blocks are required; every figure is optional.**
`tests/unit/test_added_fields_stay_optional.py` exists for fields added to an
object the frontend already builds in fixtures; it constrains what is
registered there, and these models are new. A figure may still be added to
later, so each has a default and is registered, the way a frontend-consumed
field would be. The bounds are never null, and a default would have to be a
made-up instant, so they are required like `MentorRelationshipRead`'s figures.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field

from app.domain.mentor_stats import DEFAULT_WINDOW_DAYS


class MentorStatsWindowRead(BaseModel):
    """One window's figures. Held and missed sessions belong to the window their
    `starts_at` falls in; a cancellation to the one it was made in; a review to
    the one it was written in."""

    since: dt.datetime = Field(description="Start of the window, UTC, **inclusive**.")
    until: dt.datetime = Field(description="End of the window, UTC, **exclusive**.")
    completed_sessions: int = Field(
        default=0,
        description=(
            "Sessions held: `completed`, the same rule as `completed_sessions` on "
            "your public profile. This is the dashboard's *held* count."
        ),
    )
    mentoring_minutes: int = Field(
        default=0,
        description="Scheduled minutes of those sessions, as on your public profile.",
    )
    mentees_mentored: int = Field(default=0, description="Distinct mentees across those sessions.")
    attendance_rate: int | None = Field(
        default=None,
        description=(
            "Your show-up rate, 0-100: settled sessions you attended over settled "
            "sessions you were expected at, as on your public profile. Null when "
            "no session in the window is settled."
        ),
    )
    missed_by_mentor: int = Field(
        default=0,
        description=(
            "Settled sessions in the window you were expected at and did not "
            "attend: the attendance rate's denominator less its numerator."
        ),
    )
    cancelled_by_mentor: int = Field(
        default=0,
        description=(
            "Sessions you cancelled **during the window**, whenever they were "
            "due to start. Requests you declined are not cancellations, and "
            "neither are sessions anybody else ended."
        ),
    )
    review_count: int = Field(
        default=0, description="Published reviews of you written in the window."
    )
    average_rating: float | None = Field(
        default=None,
        description=(
            "The mean of those reviews' own values (each review's overall "
            "rating, or its older valuable rating), to two places. One formula "
            "in every window, so two windows compare. Not your profile's "
            "`session_value`, whose rule changes with how many reviews you "
            "have. Null with no reviews."
        ),
    )

    @classmethod
    def of(cls, row: dict[str, Any]) -> MentorStatsWindowRead:
        value = row["average_rating"]
        rate = row["attendance_rate"]
        return cls(
            since=row["since"],
            until=row["until"],
            completed_sessions=int(row["completed_sessions"]),
            mentoring_minutes=int(row["mentoring_minutes"]),
            mentees_mentored=int(row["mentees_mentored"]),
            attendance_rate=None if rate is None else int(rate),
            missed_by_mentor=int(row["missed_by_mentor"]),
            cancelled_by_mentor=int(row["cancelled_by_mentor"]),
            review_count=int(row["review_count"]),
            average_rating=None if value is None else float(value),
        )


class MentorStatsRead(BaseModel):
    """Your figures over a window and over the equal window before it, so a
    client computes the trend. Both blocks are read in one statement."""

    window_days: int = Field(default=DEFAULT_WINDOW_DAYS, description="The window's length.")
    current: MentorStatsWindowRead = Field(description="The window ending now.")
    previous: MentorStatsWindowRead = Field(
        description="The equal window immediately before `current`."
    )
