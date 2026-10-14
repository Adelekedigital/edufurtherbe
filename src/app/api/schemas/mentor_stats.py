"""A mentor's own dashboard figures (decision 246).

Every field has a default, so none is `required` in the spec: the frontend's
fixtures build these by hand, and the project rule is optional and always sent
(`tests/unit/test_added_fields_stay_optional.py`).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field

from app.domain.mentor_stats import DEFAULT_WINDOW_DAYS


class MentorStatsWindowRead(BaseModel):
    """One window's figures. Sessions belong to the window their `starts_at`
    falls in; reviews to the window they were written in."""

    since: dt.datetime | None = Field(
        default=None,
        description="Start of the window, UTC, **inclusive**. Always sent.",
    )
    until: dt.datetime | None = Field(
        default=None,
        description="End of the window, UTC, **exclusive**. Always sent.",
    )
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
            "Settled sessions you were expected at and did not attend: the "
            "attendance rate's denominator less its numerator."
        ),
    )
    cancelled_by_mentor: int = Field(
        default=0,
        description=(
            "Sessions you cancelled. Requests you declined are not cancellations, "
            "and neither are sessions the mentee, an admin or the system ended."
        ),
    )
    review_count: int = Field(
        default=0, description="Published reviews of you written in the window."
    )
    session_value: float | None = Field(
        default=None,
        description=(
            "Average rating of those reviews, by the rule `session_value` on your "
            "public profile uses. Null with no reviews."
        ),
    )

    @classmethod
    def of(cls, row: dict[str, Any]) -> MentorStatsWindowRead:
        value = row["session_value"]
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
            session_value=None if value is None else float(value),
        )


class MentorStatsRead(BaseModel):
    """Your figures over a window and over the equal window before it, so a
    client computes the trend."""

    window_days: int = Field(default=DEFAULT_WINDOW_DAYS, description="The window's length.")
    current: MentorStatsWindowRead = Field(
        default_factory=MentorStatsWindowRead,
        description="The window ending now. Always sent.",
    )
    previous: MentorStatsWindowRead = Field(
        default_factory=MentorStatsWindowRead,
        description="The equal window immediately before `current`. Always sent.",
    )
