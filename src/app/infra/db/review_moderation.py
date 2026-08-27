"""The moderation queue, and deciding a report.

**Reading is every review; deciding is one report.** A moderator asked to judge
a complaint needs the surrounding record — one review in isolation says nothing
about whether an author is a pattern — so the queue is not restricted to
reported rows, and ``reported=true`` narrows it rather than defining it.

**Upholding is the only thing that removes anything**, and what it removes is
``reviews.deleted_at``. That column already existed for exactly this, and
`published()` — character for character the predicate of
``ix_reviews_mentor_valuable`` — is what carries the removal out to the profile
list and the average without either read learning that moderation happened.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import func, literal, select, true, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.domain.enums import ReviewReportOutcome
from app.infra.db.models.review_reports import ReviewReport
from app.infra.db.models.reviews import Review
from app.infra.db.models.user import User
from app.infra.db.qualifications import top_qualification

__all__ = ["decide_report", "list_reviews_for_moderation"]


def _after(cursor: tuple[str, UUID]) -> Any:
    """Keyset on ``(created_at, id)``, the shape every list here uses."""
    raw, after_id = cursor
    try:
        after = dt.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValidationError("cursor is not a cursor this endpoint issued") from exc
    return tuple_(Review.created_at, Review.id) < tuple_(literal(after), literal(after_id))


async def list_reviews_for_moderation(
    session: AsyncSession,
    *,
    limit: int,
    after: tuple[str, UUID] | None = None,
    reported_only: bool = False,
) -> tuple[list[dict[str, Any]], bool]:
    """One page of reviews for a moderator, newest first.

    **Not scoped by `published()`.** A withdrawn review is exactly what a
    moderator may need to look at — including one this queue withdrew — and
    hiding it here would mean the only view of a moderation decision is the one
    that cannot show its result.

    **No `LIVE` join either.** The public list drops a review whose author
    deleted their account, because a deleted user must stop being named on a
    public page. A moderator is not a public page, and a complaint about a
    review whose author has since left is still a complaint — so the author
    join is outer, and the name is simply absent when there is no live user.
    """
    author = top_qualification(Review.reviewed_by, name="author_qualification")
    report = ReviewReport.__table__.alias("report")

    statement = (
        select(
            Review.id,
            Review.created_at,
            Review.public_review,
            Review.private_review,
            Review.valuable_rating,
            Review.reviewed_for,
            (Review.deleted_at.is_not(None)).label("withdrawn"),
            User.first_name.label("author_first_name"),
            func.nullif(func.left(User.last_name, 1), "").label("author_last_initial"),
            author.c.institution.label("author_institution"),
            report.c.id.label("report_id"),
            report.c.reason.label("report_reason"),
            report.c.detail.label("report_detail"),
            report.c.created_at.label("report_created_at"),
            report.c.resolved_at.label("report_resolved_at"),
            report.c.outcome.label("report_outcome"),
        )
        .select_from(Review)
        .outerjoin(User, User.id == Review.reviewed_by)
        .outerjoin(author, true())
        .outerjoin(report, report.c.review_id == Review.id)
        .where(
            *([report.c.id.is_not(None)] if reported_only else []),
            *([_after(after)] if after is not None else []),
        )
        .order_by(Review.created_at.desc(), Review.id.desc())
        .limit(limit + 1)
    )

    rows = [dict(row) for row in (await session.execute(statement)).mappings()]
    return rows[:limit], len(rows) > limit


async def decide_report(
    session: AsyncSession, admin_id: UUID, report_id: UUID, outcome: ReviewReportOutcome
) -> Any:
    """Resolve a report, and remove the review if it is upheld. Does not commit.

    **One transaction, because a decision and its effect are one fact.** An
    upheld report whose review is still on the profile is a moderator being
    told they acted when they did not.

    **Deciding twice is refused.** A second decision would rewrite the first,
    and the record of who decided what would be whatever the last admin
    clicked — which is the opposite of what a moderation record is for. The
    `WHERE resolved_at IS NULL` is what makes that a race the database settles
    rather than a read somebody can lose.
    """
    resolved = (
        await session.execute(
            update(ReviewReport)
            .where(ReviewReport.id == report_id, ReviewReport.resolved_at.is_(None))
            .values(resolved_at=func.now(), outcome=outcome, resolved_by=admin_id)
            .returning(
                ReviewReport.id,
                ReviewReport.review_id,
                ReviewReport.reason,
                ReviewReport.created_at,
                ReviewReport.resolved_at,
                ReviewReport.outcome,
            )
        )
    ).one_or_none()

    if resolved is None:
        # Absent or already decided, and the two are distinguished: an admin
        # who cannot tell "no such report" from "somebody got there first" will
        # go looking for a bug in the wrong place.
        exists = await session.scalar(select(ReviewReport.id).where(ReviewReport.id == report_id))
        if exists is None:
            raise NotFoundError("no such report")
        raise ConflictError("that report has already been decided")

    if outcome is ReviewReportOutcome.UPHELD:
        # **The removal, and the only one in this module.** `deleted_at` rather
        # than a delete: hard-deleting evidence contradicts the append-only
        # rule as directly as an overwrite would, and the row is what the
        # decision refers to.
        #
        # Guarded on `IS NULL` so upholding a second report against a review the
        # author had already withdrawn does not move the moment it went.
        await session.execute(
            update(Review)
            .where(Review.id == resolved.review_id, Review.deleted_at.is_(None))
            .values(deleted_at=func.now())
        )

    return resolved
