"""A mentee's own dashboard home figures (decision 249).

Shaped like `/me/mentor-stats` (decision 246): every figure optional and
registered in `test_added_fields_stay_optional`; the window bounds and the two
blocks required and never null, since no default instant is true.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.api.schemas.common import COUNTED_MINUTES
from app.api.schemas.mentor_stats import WindowBoundsRead
from app.domain.mentee_stats import MISSED_DAYS, TREND_DAYS


class MenteeStatsWindowRead(WindowBoundsRead):
    """One month's held sessions, placed by their `starts_at`."""

    completed_sessions: int = Field(
        default=0, description="Sessions you received that are `completed`, starting in the window."
    )
    minutes: int = Field(default=0, description=f"Minutes of those sessions. {COUNTED_MINUTES}")

    @classmethod
    def of(cls, row: dict[str, Any]) -> MenteeStatsWindowRead:
        return cls(
            since=row["since"],
            until=row["until"],
            completed_sessions=int(row["completed_sessions"]),
            minutes=int(row["minutes"]),
        )


class MenteeStatsRead(BaseModel):
    """Your figures as a mentee, all time, plus this month against last."""

    sessions_held: int = Field(
        default=0,
        description=(
            "Sessions you received that are `completed`, all time: the same count "
            "as `mentee_completed_sessions` on `/me`."
        ),
    )
    sessions_attended: int = Field(
        default=0,
        description=(
            "Sessions you attended as a mentee, all time: you joined, whether or "
            "not your mentor did, so a session your mentor missed counts. What "
            "the invite unlock waits for (decision 295), so the Invite card's "
            '"attend a session" step reads this, not `sessions_held`.'
        ),
    )
    mentors_met: int = Field(
        default=0, description="Distinct mentors you have at least one completed session with."
    )
    minutes_of_guidance: int = Field(
        default=0,
        description=(
            "Minutes of those completed sessions, all time, like "
            f"`mentoring_minutes` on `/me/mentor-stats`; divide for hours. {COUNTED_MINUTES}"
        ),
    )
    missed_sessions: int = Field(
        default=0,
        description=(
            "Settled sessions starting in the last `missed_window_days` days that you "
            "were expected at and did not attend. A cancelled session is not "
            "missed, and neither is one only your mentor missed."
        ),
    )
    missed_window_days: int = Field(
        default=MISSED_DAYS, description="How many days back `missed_sessions` counts."
    )
    window_days: int = Field(default=TREND_DAYS, description="Each trend window's length.")
    current: MenteeStatsWindowRead = Field(description="The window ending now.")
    previous: MenteeStatsWindowRead = Field(
        description="The equal window immediately before `current`."
    )
    this_month: MenteeStatsWindowRead | None = Field(
        default=None,
        description=(
            "The UTC calendar month now is in, `[the 1st, the next 1st)`: "
            '"+2 this month". The same month as the mentor\'s monthly chart and '
            "the credit reset. Always sent; optional only so an older client's "
            "fixtures stay valid."
        ),
    )

    @classmethod
    def of(cls, row: dict[str, Any]) -> MenteeStatsRead:
        return cls(
            sessions_held=int(row["sessions_held"]),
            sessions_attended=int(row["sessions_attended"]),
            mentors_met=int(row["mentors_met"]),
            minutes_of_guidance=int(row["minutes_of_guidance"]),
            missed_sessions=int(row["missed_sessions"]),
            missed_window_days=MISSED_DAYS,
            window_days=TREND_DAYS,
            current=MenteeStatsWindowRead.of(row["current"]),
            previous=MenteeStatsWindowRead.of(row["previous"]),
            this_month=MenteeStatsWindowRead.of(row["this_month"]),
        )
