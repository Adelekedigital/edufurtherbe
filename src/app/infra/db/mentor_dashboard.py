"""A mentor's own figures over a window, for their dashboard (decision 246).

**Composed, never re-derived** (non-negotiable #8), the shape
`mentor_relationship` set: the session figures are
`session_stats.mentor_window_figures()`, the same statement the public profile's
all-time figures come from with a window applied, and the review figures are
`review_stats.window_summary()`, over `published()` and `review_value()`.

**Scoped to the caller in the statement** (non-negotiable #5): every clause
names the mentor, so another mentor's rows are never read. A caller who is not a
mentor matches nothing and reads as zeros and nulls, as `/me/session-types`
answers a non-mentor with an empty page.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import literal, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.mentor_stats import Window
from app.infra.db.review_stats import window_summary
from app.infra.db.session_stats import mentor_window_figures

__all__ = ["mentor_window_stats"]


async def mentor_window_stats(
    session: AsyncSession, mentor: UUID, windows: Sequence[Window]
) -> list[dict[str, Any]]:
    """Every dashboard figure for each of ``windows``, in their order.

    **One statement for all of them**, a `UNION ALL` of one row per window, so
    every window reads one snapshot: a session settling between two reads
    would otherwise skew the trend the client computes from them.
    """
    rows = (await session.execute(_statement(mentor, windows))).mappings().all()
    return [
        {**row, "since": window.since, "until": window.until}
        for row, window in zip(rows, windows, strict=True)
    ]


def _statement(mentor: UUID, windows: Sequence[Window]) -> Any:
    """One row per window, in the order given: `UNION ALL` promises no order,
    so each row carries its position and the outer select sorts on it."""
    parts = []
    for position, window in enumerate(windows):
        review_count, average_rating = window_summary(mentor, window)
        parts.append(
            mentor_window_figures(mentor, window).add_columns(
                review_count.label("review_count"),
                average_rating.label("average_rating"),
                literal(position).label("position"),
            )
        )
    statement = union_all(*parts).subquery("windows")
    return statement.select().order_by(statement.c.position)
