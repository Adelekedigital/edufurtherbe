"""One mentee's history with one mentor, for the profile's review prompts.

**Composed, never re-derived.** Each figure is read through the predicate its
own endpoint already answers with, so a prompt cannot disagree with the list it
points at (non-negotiable #8):

- the count is `session_stats.received()` — the same rule as `/me`'s
  `mentee_completed_sessions` — narrowed to this mentor;
- the last review is `review_stats.published()` — the same rule as the
  mentor's public figures — narrowed to this author;
- `review_due` is `review_eligibility.reviewable_sessions()` for this mentor —
  literally the query behind `/me/reviewable-sessions?mentor_id=`, asked for
  existence;
- `review_update_due` is `domain.reviews.review_update_due()` over
  `review_due`, that last review and the count of `received()` sessions with
  this mentor whose `starts_at` is after it — the same completed rule as the
  count, and the same time column `held()` places a session by. It implies
  `review_due`: an update is a new review (option A, 2026-10-21).

**Scoped to the caller in the statement** (non-negotiable #5): every clause
names the mentee, so there is no row belonging to somebody else to filter out
afterwards. An id that is nobody, or not a mentor, simply matches nothing and
reads as no history — a `404` would tell a caller which ids are mentors.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.reviews import review_update_due
from app.infra.db.models.reviews import Review
from app.infra.db.models.sessions import Session
from app.infra.db.review_eligibility import reviewable_sessions
from app.infra.db.review_stats import published
from app.infra.db.session_stats import received

__all__ = ["mentor_relationship"]


async def mentor_relationship(
    session: AsyncSession, mentee: UUID, mentor: UUID, *, now: dt.datetime
) -> dict[str, Any]:
    """`completed_sessions_with_mentor`, `last_reviewed_at`, `review_due` and
    `review_update_due`.

    One statement of scalar subqueries, so the figures are read from one
    snapshot and cannot describe two different moments.
    """
    from_mentor = and_(received(mentee), Session.mentor_id == mentor)
    completed = select(func.count()).select_from(Session).where(from_mentor).scalar_subquery()
    last_reviewed = (
        select(func.max(Review.created_at))
        .where(and_(published(mentor), Review.reviewed_by == mentee))
        .scalar_subquery()
    )
    due = reviewable_sessions(mentee, now, mentor).exists()
    since_review = (
        select(func.count())
        .select_from(Session)
        .where(from_mentor, Session.starts_at > last_reviewed)
        .scalar_subquery()
    )

    row = (
        await session.execute(
            select(
                completed.label("completed_sessions_with_mentor"),
                last_reviewed.label("last_reviewed_at"),
                due.label("review_due"),
                since_review.label("sessions_since_review"),
            )
        )
    ).one()
    figures = dict(row._mapping)
    since = figures.pop("sessions_since_review")
    figures["review_update_due"] = review_update_due(
        figures["last_reviewed_at"], since, now, review_due=figures["review_due"]
    )
    return figures
