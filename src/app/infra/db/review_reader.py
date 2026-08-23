"""Reading a single review back, scoped to the person who wrote it.

One function, and it exists so that `POST` and `PATCH` return the same shape
built by the same code — `booked_session` reads its session back the same way
and for the same reason. Assembling the response from the values just written
would drift from this the first time a column gained a default.

**Withdrawn reviews are absent.** A review taken down by moderation is not the
author's to read back or edit, so `deleted_at IS NULL` is part of the scope
rather than a filter applied afterwards. The eligibility clauses deliberately do
*not* filter it — a withdrawn review still happened, and still holds its session's
slot — which is the same rule seen from the other side.

**The scope is in the `WHERE`.** Non-negotiable #5, on the read path as much as
the write: a review that is not the caller's comes back absent rather than
fetched and then refused, which is what lets the route answer `404` without a
`403` confirming the row exists.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.db.models.reviews import Review
from app.infra.db.models.user import User
from app.infra.db.qualifications import top_qualification
from app.infra.db.review_stats import published

__all__ = ["get_review_row", "list_mentor_reviews"]


async def get_review_row(
    session: AsyncSession, review_id: UUID, author: UUID
) -> dict[str, Any] | None:
    """One review of the caller's own, or ``None``.

    ``private_review`` is selected and `ReviewRead` drops it, rather than being
    omitted here. The author is entitled to read back what they just wrote —
    including the platform feedback — and the model is where "never published"
    is enforced, once, for every caller.
    """
    row = (
        await session.execute(
            select(
                Review.id,
                Review.session_id,
                Review.reviewed_for,
                Review.communication_rating,
                Review.knowledge_rating,
                Review.practicality_rating,
                Review.support_rating,
                Review.valuable_rating,
                Review.nps_recommend_score,
                Review.public_review,
                Review.private_review,
                Review.created_at,
                Review.updated_at,
            ).where(
                Review.id == review_id,
                Review.reviewed_by == author,
                Review.deleted_at.is_(None),
            )
        )
    ).mappings()
    found = row.one_or_none()
    return dict(found) if found is not None else None


async def list_mentor_reviews(
    session: AsyncSession,
    mentor: UUID,
    *,
    limit: int,
    after: UUID | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """One page of a mentor's published reviews, newest first.

    **The id is the cursor**, which is ADR 0016's base case rather than a
    shortcut: `reviews.id` is a UUIDv7, so id order *is* creation order, and a
    separate sort key would be the same fact twice.

    **The surname never leaves the database.** `left(last_name, 1)` is computed
    in SQL, so the column is not selected at all — a review is public and
    attributed, and "Fauziyah F." is the attribution the product chose. A read
    model dropping the surname would still have fetched it, and the next person
    to add a field would find it sitting there.

    The reviewer's institution comes from `top_qualification`, the same lateral
    the discovery card uses for a mentor's own degree, pointed at the author.
    Two copies of *which* institution represents somebody would drift, and the
    copy with fewer tests is the one that would.

    Scoped by `published()`, so a withdrawn review is absent here exactly as it
    is absent from the averages — the one thing withdrawal is *for*.
    """
    author = top_qualification(Review.reviewed_by, name="author_qualification")
    statement = (
        select(
            Review.id,
            Review.created_at,
            Review.public_review,
            Review.valuable_rating,
            User.first_name.label("author_first_name"),
            func.left(User.last_name, 1).label("author_last_initial"),
            author.c.institution.label("author_institution"),
        )
        .select_from(Review)
        .join(User, User.id == Review.reviewed_by)
        .outerjoin(author, true())
        .where(published(mentor), *([Review.id < after] if after is not None else []))
        .order_by(Review.id.desc())
        .limit(limit + 1)
    )
    rows = [dict(row) for row in (await session.execute(statement)).mappings()]
    return rows[:limit], len(rows) > limit
