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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.db.models.reviews import Review

__all__ = ["get_review_row"]


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
