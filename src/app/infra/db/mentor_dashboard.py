"""A mentor's own figures over a window, for their dashboard (decision 246),
and per calendar month, for its chart (decision 247).

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

from sqlalchemy import TIMESTAMP, Integer, column, func, literal, select, union_all, values
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.mentor_stats import Window
from app.infra.db.predicates import during
from app.infra.db.review_stats import window_summary
from app.infra.db.session_stats import (
    BY_MENTEE,
    BY_MENTOR,
    BY_OTHER,
    cancellations,
    current_streak,
    held_count,
    mentor_window_figures,
    missed_count,
)

__all__ = ["mentor_monthly_stats", "mentor_window_stats"]


async def mentor_window_stats(
    session: AsyncSession, mentor: UUID, windows: Sequence[Window]
) -> list[dict[str, Any]]:
    """Every dashboard figure for each of ``windows``, in their order, each row
    also carrying the mentor's `current_streak` (decision 250), which no window
    bounds.

    **One statement for all of them**, a `UNION ALL` of one row per window, so
    every window reads one snapshot: a session settling between two reads
    would otherwise skew the trend the client computes from them, or the
    streak beside it.
    """
    rows = (await session.execute(_statement(mentor, windows))).mappings().all()
    return [
        {**row, "since": window.since, "until": window.until}
        for row, window in zip(rows, windows, strict=True)
    ]


def _statement(mentor: UUID, windows: Sequence[Window]) -> Any:
    """One row per window, in the order given: `UNION ALL` promises no order,
    so each row carries its position and the outer select sorts on it. The
    streak is an uncorrelated scalar subquery on the outer select, so it runs
    once (an `InitPlan`), not once per window."""
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
    return (
        statement.select()
        .add_columns(current_streak(mentor).label("current_streak"))
        .order_by(statement.c.position)
    )


async def mentor_monthly_stats(
    session: AsyncSession, mentor: UUID, months: Sequence[Window]
) -> list[dict[str, Any]]:
    """The series' figures for each of ``months``, in their order (decision 247).

    The same predicates as a dashboard window, with each month's edges as the
    window: held and missed by `starts_at`, a cancellation by when it was made
    and split by `canceller`. One statement, so every month reads one snapshot,
    and a month with nothing in it is still a row. Each row carries its
    window's own edges and `month` label, so the label is spelled once.
    """
    rows = (await session.execute(_monthly(mentor, months))).mappings().all()
    return [
        {**row, "month": window.month, "since": window.since, "until": window.until}
        for row, window in zip(rows, months, strict=True)
    ]


def _monthly(mentor: UUID, months: Sequence[Window]) -> Any:
    """The months as a `VALUES` list, each row's counts beside it.

    **`VALUES`, not `generate_series`**: the months come from
    `domain.mentor_stats.month_windows`, the one rule for a month's edges;
    stepping a `timestamptz` by `interval '1 month'` in SQL would be a second,
    and one that follows the connection's `TimeZone` rather than UTC.

    **Held and missed are per-month scalar subqueries**, each an index scan
    (`ix_sessions_mentor_completed` with `ix_sessions_starts_at`). A single
    grouped join of the mentor's sessions to the months was measured slower
    and, from a generic estimate over the month columns, tipped into JIT
    (decision 247). Cancellations are read once (one `LATERAL` per cancelled
    session) and joined to the month they were made in by `during()`.
    """
    bounds = (
        values(
            column("position", Integer),
            column("since", TIMESTAMP(timezone=True)),
            column("until", TIMESTAMP(timezone=True)),
            name="months",
        )
        .data([(position, w.since, w.until) for position, w in enumerate(months)])
        .alias("months")
    )
    month = bounds.c
    cancelled = cancellations(mentor, None)

    def by(canceller: str) -> Any:
        return func.count().filter(cancelled.c.canceller == canceller)

    return (
        select(
            held_count(mentor, month).label("completed_sessions"),
            missed_count(mentor, month).label("missed_by_mentor"),
            by(BY_MENTOR).label("cancelled_by_mentor"),
            by(BY_MENTEE).label("cancelled_by_mentee"),
            by(BY_OTHER).label("cancelled_by_other"),
        )
        .select_from(bounds.outerjoin(cancelled, during(cancelled.c.cancelled_at, month)))
        .group_by(month.position, month.since, month.until)
        .order_by(month.position)
    )
