"""Filing a report against a review of yourself.

**The composite key is the authorization**, and this module's job is to make it
a 404 the caller can act on rather than an `IntegrityError` they cannot. The
guarantee lives in `fk_review_reports_report_belongs_to_subject`; a check here
that disagreed with it would simply be wrong.

**404, never 403.** A review the caller may not report is reported as absent,
because 403 confirms the row exists and turns an authorization answer into an
enumeration oracle — the same rule `require_admin` follows for a non-admin.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.domain.enums import ReviewReportReason
from app.infra.db.models.review_reports import ReviewReport
from app.infra.db.models.reviews import Review

__all__ = ["report_review"]


async def report_review(
    session: AsyncSession,
    subject_id: UUID,
    review_id: UUID,
    *,
    reason: ReviewReportReason,
    detail: str | None,
) -> Any:
    """File a report and return it. Does not commit.

    **Ownership is scoped in the query**, not checked after fetching: the
    `WHERE` names both the review and the caller, so a review about somebody
    else is simply not found.

    A withdrawn review is still reportable. It may have been withdrawn by its
    author rather than by moderation, and a subject who wants the record
    examined should not be blocked by the author having second-guessed
    themselves first.
    """
    target = await session.scalar(
        select(Review.id).where(Review.id == review_id, Review.reviewed_for == subject_id)
    )
    if target is None:
        raise NotFoundError("no such review")

    # **The insert arbitrates the duplicate**, not a prior read: two taps on a
    # report button both see nothing and both insert, and the second violates
    # `uq_review_reports_review_id_reporter` — which is not in `STATUS_BY_ERROR`,
    # so the caller would get a 500 instead of being told they already reported
    # it.
    filed = (
        await session.execute(
            pg_insert(ReviewReport)
            .values(
                review_id=review_id,
                reported_by=subject_id,
                reason=reason,
                detail=detail,
            )
            .on_conflict_do_nothing(index_elements=["review_id", "reported_by"])
            .returning(
                ReviewReport.id,
                ReviewReport.review_id,
                ReviewReport.reason,
                ReviewReport.detail,
                ReviewReport.created_at,
                ReviewReport.resolved_at,
                ReviewReport.outcome,
            )
        )
    ).one_or_none()

    if filed is None:
        raise ConflictError("you have already reported this review")

    return filed
