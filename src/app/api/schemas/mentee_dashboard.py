"""A mentee's own dashboard home figures (decision 249).

Shaped like `/me/mentor-stats` (decision 246): every figure optional and
registered in `test_added_fields_stay_optional`; the window bounds and the two
blocks required and never null, since no default instant is true.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.api.schemas.mentor_stats import WindowBoundsRead
from app.domain.mentee_dashboard import MISSED_DAYS, TREND_DAYS


class MenteeDashboardWindowRead(WindowBoundsRead):
    """One month's held sessions, placed by their `starts_at`."""

    completed_sessions: int = Field(
        default=0, description="Sessions you received that are `completed`, starting in the window."
    )
    minutes: int = Field(default=0, description="Scheduled minutes of those sessions.")

    @classmethod
    def of(cls, row: dict[str, Any]) -> MenteeDashboardWindowRead:
        return cls(
            since=row["since"],
            until=row["until"],
            completed_sessions=int(row["completed_sessions"]),
            minutes=int(row["minutes"]),
        )


class MenteeDashboardRead(BaseModel):
    """Your figures as a mentee, all time, plus this month against last."""

    sessions_held: int = Field(
        default=0,
        description=(
            "Sessions you received that are `completed`, all time: the same count "
            "as `mentee_completed_sessions` on `/me`."
        ),
    )
    mentors_met: int = Field(
        default=0, description="Distinct mentors you have at least one completed session with."
    )
    minutes_of_guidance: int = Field(
        default=0,
        description=(
            "Scheduled minutes of those completed sessions, all time. Minutes, "
            "like `mentoring_minutes` on `/me/mentor-stats`; divide for hours."
        ),
    )
    missed_sessions_90d: int = Field(
        default=0,
        description=(
            f"Settled sessions starting in the last {MISSED_DAYS} days that you "
            "were expected at and did not attend. A cancelled session is not "
            "missed, and neither is one only your mentor missed."
        ),
    )
    window_days: int = Field(default=TREND_DAYS, description="Each trend window's length.")
    current: MenteeDashboardWindowRead = Field(description="The window ending now.")
    previous: MenteeDashboardWindowRead = Field(
        description="The equal window immediately before `current`."
    )

    @classmethod
    def of(cls, row: dict[str, Any]) -> MenteeDashboardRead:
        return cls(
            sessions_held=int(row["sessions_held"]),
            mentors_met=int(row["mentors_met"]),
            minutes_of_guidance=int(row["minutes_of_guidance"]),
            missed_sessions_90d=int(row["missed_sessions_90d"]),
            window_days=TREND_DAYS,
            current=MenteeDashboardWindowRead.of(row["current"]),
            previous=MenteeDashboardWindowRead.of(row["previous"]),
        )
