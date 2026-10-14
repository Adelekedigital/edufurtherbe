"""A mentee's own dashboard home figures (decision 249).

**Composed, never re-derived** (non-negotiable #8): every figure is a
`session_stats` builder the mentor dashboard also runs, from the other side.
Held is `received()`, the predicate `/me`'s `mentee_completed_sessions` counts;
minutes and distinct mentors are `_held()`, which the profile's figures read;
missed is `missed_by()` on the mentee's participant row; every window is
`predicates.during`.

**Scoped to the caller in the statement** (non-negotiable #5): every clause
names the mentee, so another user's rows, and the caller's own sessions as a
mentor, are never read. Anyone who has received nothing reads zeros.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.mentee_dashboard import DashboardWindows
from app.infra.db.session_stats import mentee_missed, mentee_window_figures

__all__ = ["mentee_dashboard"]


async def mentee_dashboard(
    session: AsyncSession, mentee: UUID, windows: DashboardWindows
) -> dict[str, Any]:
    """Every figure, all time and per window, in **one statement**.

    One snapshot, the property `/me/mentor-stats`'s `UNION ALL` buys: a session
    settling mid-read cannot be in the all-time count and missing from this
    month's. The figures here are not one shape per window, so each window is a
    one-row aggregate and the statement cross-joins them into one row.
    """
    row = (await session.execute(_statement(mentee, windows))).mappings().one()
    return {
        "sessions_held": row["completed_sessions"],
        "mentors_met": row["mentors_met"],
        "minutes_of_guidance": row["minutes"],
        "missed_sessions_90d": row["missed"],
        "current": {
            "since": windows.current.since,
            "until": windows.current.until,
            "completed_sessions": row["current_sessions"],
            "minutes": row["current_minutes"],
        },
        "previous": {
            "since": windows.previous.since,
            "until": windows.previous.until,
            "completed_sessions": row["previous_sessions"],
            "minutes": row["previous_minutes"],
        },
    }


def _statement(mentee: UUID, windows: DashboardWindows) -> Any:
    all_time = mentee_window_figures(mentee, None).subquery("all_time")
    current = mentee_window_figures(mentee, windows.current).subquery("current")
    previous = mentee_window_figures(mentee, windows.previous).subquery("previous")
    return (
        select(
            all_time.c.completed_sessions,
            all_time.c.minutes,
            all_time.c.mentors_met,
            mentee_missed(mentee, windows.missed).label("missed"),
            current.c.completed_sessions.label("current_sessions"),
            current.c.minutes.label("current_minutes"),
            previous.c.completed_sessions.label("previous_sessions"),
            previous.c.minutes.label("previous_minutes"),
        )
        # Each side is one aggregate row, so the cross join is one row.
        .select_from(all_time)
        .join(current, true())
        .join(previous, true())
    )
