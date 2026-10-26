"""A mentee's own dashboard home figures (decision 249).

**Composed, never re-derived** (non-negotiable #8): every figure is a
`session_stats` builder the mentor dashboard also runs, from the other side.
Held is `received()`, the predicate `/me`'s `mentee_completed_sessions` counts;
minutes and distinct mentors are `_held()`, which the profile's figures read;
missed is `missed_by()` and attended `attended_by()` on the mentee's
participant row; every window is
`predicates.during`.

**Scoped to the caller in the statement** (non-negotiable #5): the scan reads
only the mentee's sessions, and each figure's filter names them again, so
another user's rows, and the caller's own sessions as a mentor, are never
counted. Anyone who has received nothing reads zeros.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.mentee_stats import DashboardWindows
from app.infra.db.models.sessions import Session
from app.infra.db.session_stats import mentee_attended, mentee_missed, mentee_window_figures

__all__ = ["mentee_dashboard"]


async def mentee_dashboard(
    session: AsyncSession, mentee: UUID, windows: DashboardWindows
) -> dict[str, Any]:
    """Every figure, all time and per window, in **one statement and one scan**
    of the mentee's sessions: one snapshot, the property `/me/mentor-stats`'s
    `UNION ALL` buys, so a session settling mid-read cannot be in the all-time
    count and missing from this month's. Each figure is an aggregate `FILTER`ed
    to its window."""
    row = (await session.execute(_statement(mentee, windows))).mappings().one()
    return {
        "sessions_held": row["completed_sessions"],
        "sessions_attended": row["attended"],
        "mentors_met": row["mentors_met"],
        "minutes_of_guidance": row["minutes"],
        "missed_sessions": row["missed"],
        "current": {**windows.current._asdict(), **_block(row, "current")},
        "previous": {**windows.previous._asdict(), **_block(row, "previous")},
        "this_month": {**windows.month._asdict(), **_block(row, "month")},
    }


def _block(row: Any, name: str) -> dict[str, Any]:
    return {"completed_sessions": row[f"{name}_sessions"], "minutes": row[f"{name}_minutes"]}


def _statement(mentee: UUID, windows: DashboardWindows) -> Any:
    held, minutes, mentors = mentee_window_figures(mentee, None)
    current_held, current_minutes, _ = mentee_window_figures(mentee, windows.current)
    previous_held, previous_minutes, _ = mentee_window_figures(mentee, windows.previous)
    month_held, month_minutes, _ = mentee_window_figures(mentee, windows.month)
    return (
        select(
            held.label("completed_sessions"),
            minutes.label("minutes"),
            mentors.label("mentors_met"),
            mentee_missed(mentee, windows.missed).label("missed"),
            mentee_attended(mentee).label("attended"),
            current_held.label("current_sessions"),
            current_minutes.label("current_minutes"),
            previous_held.label("previous_sessions"),
            previous_minutes.label("previous_minutes"),
            month_held.label("month_sessions"),
            month_minutes.label("month_minutes"),
        )
        .select_from(Session)
        # The one scan: the caller's sessions as a mentee
        # (`uq_sessions_mentee_id_id`). Every figure's filter names the mentee
        # too, so dropping this changes no number (an equivalent mutant) but
        # turns an index scan of their rows into a scan of every session.
        .where(Session.mentee_id == mentee)
    )
