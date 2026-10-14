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

from app.domain.mentor_stats import DEFAULT_MONTHS, DEFAULT_WINDOW_DAYS


class WindowBoundsRead(BaseModel):
    """A dashboard window's edges, `[since, until)` in UTC: one definition for
    every dashboard (decisions 246, 249), so they cannot disagree about an edge."""

    since: dt.datetime = Field(description="Start of the window, UTC, **inclusive**.")
    until: dt.datetime = Field(description="End of the window, UTC, **exclusive**.")


class MentorStatsWindowRead(WindowBoundsRead):
    """One window's figures. Held and missed sessions belong to the window their
    `starts_at` falls in; a cancellation to the one it was made in; a review to
    the one it was written in."""

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
    current_streak: int | None = Field(
        default=None,
        description=(
            "Sessions you have held in a row since you last missed one or "
            "cancelled one yourself (a cancellation counts from when you made "
            "it), all time rather than in a window; nothing else (a cancellation "
            "by anyone else, a mentee's no-show, a session not yet held) adds "
            "to it or resets it, and it is 0 when you have held none since."
        ),
    )
    current: MentorStatsWindowRead = Field(description="The window ending now.")
    previous: MentorStatsWindowRead = Field(
        description="The equal window immediately before `current`."
    )


class MentorMonthRead(BaseModel):
    """One UTC calendar month of the chart (decision 247): the same figures as
    a dashboard window, with the month as the window."""

    month: str = Field(description="The month, `YYYY-MM`, UTC.", examples=["2026-10"])
    since: dt.datetime = Field(description="Start of the month, UTC, **inclusive**.")
    until: dt.datetime = Field(description="Start of the next month, UTC, **exclusive**.")
    completed_sessions: int = Field(
        default=0,
        description=(
            "Sessions held: `completed`, starting in the month. The same rule as "
            "`completed_sessions` in `/me/mentor-stats`."
        ),
    )
    missed_by_mentor: int = Field(
        default=0,
        description=(
            "Settled sessions starting in the month you were expected at and did "
            "not attend, as in `/me/mentor-stats`."
        ),
    )
    cancelled_by_mentor: int = Field(
        default=0,
        description="Sessions you cancelled **during the month**, whenever they were due.",
    )
    cancelled_by_mentee: int = Field(
        default=0,
        description="Sessions of yours your mentee cancelled during the month.",
    )
    cancelled_by_other: int = Field(
        default=0,
        description=(
            "Sessions of yours cancelled during the month by anybody else: the "
            "platform, an admin, or nobody recorded."
        ),
    )

    @classmethod
    def of(cls, row: dict[str, Any]) -> MentorMonthRead:
        return cls(
            month=row["month"],
            since=row["since"],
            until=row["until"],
            completed_sessions=int(row["completed_sessions"]),
            missed_by_mentor=int(row["missed_by_mentor"]),
            cancelled_by_mentor=int(row["cancelled_by_mentor"]),
            cancelled_by_mentee=int(row["cancelled_by_mentee"]),
            cancelled_by_other=int(row["cancelled_by_other"]),
        )


class MentorMonthlyStatsRead(BaseModel):
    """Your figures per UTC calendar month, oldest first, every month present."""

    months: int = Field(default=DEFAULT_MONTHS, description="How many months `series` holds.")
    series: list[MentorMonthRead] = Field(
        description=(
            "One entry per month, oldest first, ending with this month; a month "
            "with nothing in it is zeros, never missing."
        )
    )
