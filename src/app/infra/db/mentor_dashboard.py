"""A mentor's own figures over a window, for their dashboard (decision 246).

**Composed, never re-derived** (non-negotiable #8), the shape
`mentor_relationship` set: the session figures are
`session_stats.mentor_window_figures()`, the same statement the public profile's
all-time figures come from with a window applied, and the review figures are
`review_stats.card_summary()` narrowed to reviews written in the window.

**Scoped to the caller in the statement** (non-negotiable #5): every clause
names the mentor, so another mentor's rows are never read. A caller who is not a
mentor matches nothing and reads as zeros and nulls, as `/me/session-types`
answers a non-mentor with an empty page.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.mentor_stats import Window
from app.infra.db.review_stats import card_summary
from app.infra.db.session_stats import mentor_window_figures

__all__ = ["mentor_window_stats"]


async def mentor_window_stats(
    session: AsyncSession, mentor: UUID, window: Window
) -> dict[str, Any]:
    """Every dashboard figure for sessions starting, and reviews written, in
    ``window``. One statement, so every figure reads one snapshot."""
    figures = mentor_window_figures(mentor, window).subquery("sessions_in_window")
    review_count, session_value = card_summary(mentor, window)
    statement = select(
        *figures.c,
        review_count.scalar_subquery().label("review_count"),
        session_value.scalar_subquery().label("session_value"),
    )
    row = (await session.execute(statement)).mappings().one()
    return {**row, "since": window.since, "until": window.until}
