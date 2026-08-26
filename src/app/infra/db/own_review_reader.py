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

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, literal, select, true, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationError
from app.infra.db.models.review_reports import ReviewReport
from app.infra.db.models.reviews import Review
from app.infra.db.models.user import User
from app.infra.db.predicates import LIVE
from app.infra.db.qualifications import top_qualification

__all__ = ["list_reviews_about"]


def _after(cursor: tuple[str, UUID]) -> Any:
    """Keyset, on the sort column plus the id — ADR 0016's amended form.

    The same shape `review_reader._after` uses, and for the same reason: the
    sort key is a timestamp rendered as text, so a token that survives base64
    while holding something that is not a timestamp is a **client** error.
    Raising here turns a 500 into the 422 the envelope documents.
    """
    raw, after_id = cursor
    try:
        after = dt.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValidationError("cursor is not a cursor this endpoint issued") from exc
    # Descending, so the page moves *backwards* through time.
    return tuple_(Review.created_at, Review.id) < tuple_(literal(after), literal(after_id))


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
    """
    author = top_qualification(Review.reviewed_by, name="author_qualification")
    mine = ReviewReport.__table__.alias("mine")

    statement = (
        select(
            Review.id,
            Review.created_at,
            Review.public_review,
            Review.valuable_rating,
            # The subject sees that a review went, which the public list cannot
            # show and which they would otherwise learn only by its absence.
            (Review.deleted_at.is_not(None)).label("withdrawn"),
            User.first_name.label("author_first_name"),
            func.nullif(func.left(User.last_name, 1), "").label("author_last_initial"),
            author.c.institution.label("author_institution"),
            mine.c.id.label("report_id"),
            mine.c.reason.label("report_reason"),
            mine.c.created_at.label("report_created_at"),
            mine.c.resolved_at.label("report_resolved_at"),
            mine.c.outcome.label("report_outcome"),
        )
        .select_from(Review)
        # `LIVE` for the same reason the public list has it: a reviewer who
        # deletes their account stops being named, here as well as there.
        .join(User, and_(User.id == Review.reviewed_by, LIVE))
        .outerjoin(author, true())
        .outerjoin(
            mine,
            and_(mine.c.review_id == Review.id, mine.c.reported_by == subject),
        )
        .where(
            Review.reviewed_for == subject,
            *([_after(after)] if after is not None else []),
        )
        .order_by(Review.created_at.desc(), Review.id.desc())
        .limit(limit + 1)
    )

    rows = [dict(row) for row in (await session.execute(statement)).mappings()]
    return rows[:limit], len(rows) > limit
