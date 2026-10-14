"""Reviews *about* the caller, as the subject sees them.

**Deliberately not `published()`.** The public list hides a withdrawn review,
which is the whole point of withdrawal — but the subject is the one person who
needs to see that it went, and whether it went because they reported it or
because its author took it back. A queue you cannot see the outcome of is a
queue nobody trusts.

So this reads every review naming the caller as its subject, carries
``withdrawn`` alongside, and joins the caller's own report if there is one.

**The report join is scoped to the caller's report**, not to any report on the
review. Reports are one-per-person, and a subject has no business seeing that
somebody else also complained — today only the subject can report, so the two
are the same set, but writing the join loosely is how that stops being true
without anybody noticing.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.db import keyset
from app.infra.db.models.review_reports import ReviewReport
from app.infra.db.models.reviews import Review
from app.infra.db.review_authors import author_columns, author_session_id, with_author
from app.infra.db.review_stats import review_value
from app.infra.db.review_topics import topic_columns, with_topic

__all__ = ["list_reviews_about"]


async def list_reviews_about(
    session: AsyncSession,
    subject: UUID,
    *,
    limit: int,
    after: tuple[str, UUID] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """One page of the reviews written about ``subject``, newest first.

    Ordered on ``created_at`` with the id breaking ties, for the reason
    `list_mentor_reviews` records: the 53 migrated reviews take their id at load
    time while carrying a backfilled ``created_at``, so an id-ordered list would
    put a 2023 review at the top of "newest first".

    The surname never leaves the database — ``left(last_name, 1)`` in SQL, the
    same attribution the public page uses. The subject does not get a fuller
    name than a stranger does: knowing who reviewed you is the product's choice
    already, and widening it here would be a second answer to one question.

    Each row names the offering it was about through `review_topics`, the join
    the public list uses, so the two lists cannot disagree on it (decision 251).
    """
    mine = ReviewReport.__table__.alias("mine")

    statement = (
        # The same author join as the public list: a reviewer who deletes their
        # account stops being named, here as well as there, and their review
        # stays in both.
        with_topic(
            with_author(
                select(
                    Review.id,
                    Review.created_at,
                    Review.public_review,
                    author_session_id(),
                    Review.reviewed_for,
                    review_value().label("session_value"),
                    # The subject sees that a review went, which the public list cannot
                    # show and which they would otherwise learn only by its absence.
                    (Review.deleted_at.is_not(None)).label("withdrawn"),
                    *topic_columns(),
                    *author_columns(),
                    mine.c.id.label("report_id"),
                    mine.c.reason.label("report_reason"),
                    mine.c.created_at.label("report_created_at"),
                    mine.c.resolved_at.label("report_resolved_at"),
                    mine.c.outcome.label("report_outcome"),
                ).select_from(Review)
            )
        )
        .outerjoin(
            mine,
            and_(mine.c.review_id == Review.id, mine.c.reported_by == subject),
        )
        .where(
            Review.reviewed_for == subject,
            *([keyset.after(Review.created_at, Review.id, after)] if after is not None else []),
        )
        .order_by(Review.created_at.desc(), Review.id.desc())
        .limit(limit + 1)
    )

    rows = [dict(row) for row in (await session.execute(statement)).mappings()]
    return rows[:limit], len(rows) > limit
