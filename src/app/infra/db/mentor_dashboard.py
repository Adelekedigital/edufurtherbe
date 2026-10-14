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

from sqlalchemy import TIMESTAMP, Integer, and_, column, func, literal, select, union_all, values
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.mentor_stats import Window
from app.infra.db.models.sessions import Session
from app.infra.db.predicates import during
from app.infra.db.review_stats import window_summary
from app.infra.db.session_stats import (
    BY_MENTEE,
    BY_MENTOR,
    BY_OTHER,
    cancellations,
    held,
    mentor_window_figures,
    missed,
)

__all__ = ["mentor_monthly_stats", "mentor_window_stats"]


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
    """The months as a `VALUES` list, with one grouped pass over the mentor's
    sessions and one over their cancellations, joined by position.

    **`VALUES`, not `generate_series`**: the months come from
    `domain.mentor_stats.month_windows`, the one rule for a month's edges;
    stepping a `timestamptz` by `interval '1 month'` in SQL would be a second,
    and one that follows the connection's `TimeZone` rather than UTC.

    **Two passes, not one join**: a session and a cancellation land in
    different months (a cancellation is placed when it was made), so joining
    both to a month in one `GROUP BY` would multiply one by the other. Each
    pass reads its rows once and buckets them with `predicates.during`; the
    figures are `FILTER`s by the window's own predicates (`held()`,
    `missed()`, `canceller`).
    """
    bounds = (
        values(
            column("position", Integer),
            column("since", TIMESTAMP(timezone=True)),
            column("until", TIMESTAMP(timezone=True)),
            name="months",
        )
        .data([(position, w.since, w.until) for position, w in enumerate(months)])
        .cte("months")
    )
    month = bounds.c
    sessions = (
        select(
            month.position,
            func.count().filter(held(mentor, month)).label("completed_sessions"),
            func.count().filter(missed(mentor, month)).label("missed_by_mentor"),
        )
        .select_from(
            bounds.outerjoin(
                Session, and_(Session.mentor_id == mentor, during(Session.starts_at, month))
            )
        )
        .group_by(month.position)
        .subquery("held")
    )
    cancelled = cancellations(mentor, None)

    def by(canceller: str) -> Any:
        return func.count().filter(cancelled.c.canceller == canceller)

    cancels = (
        select(
            month.position,
            by(BY_MENTOR).label("cancelled_by_mentor"),
            by(BY_MENTEE).label("cancelled_by_mentee"),
            by(BY_OTHER).label("cancelled_by_other"),
        )
        .select_from(bounds.outerjoin(cancelled, during(cancelled.c.cancelled_at, month)))
        .group_by(month.position)
        .subquery("cancels")
    )
    return (
        select(
            sessions.c.completed_sessions,
            sessions.c.missed_by_mentor,
            cancels.c.cancelled_by_mentor,
            cancels.c.cancelled_by_mentee,
            cancels.c.cancelled_by_other,
        )
        .join_from(sessions, cancels, cancels.c.position == sessions.c.position)
        .order_by(sessions.c.position)
    )
