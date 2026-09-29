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

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import and_, literal, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationError
from app.infra.db.models.reviews import Review
from app.infra.db.models.sessions import Session, SessionType
from app.infra.db.review_authors import author_columns, with_author
from app.infra.db.review_stats import published, review_value

__all__ = ["get_review_row", "list_authored_reviews", "list_mentor_reviews"]


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
                Review.overall_rating,
                Review.nps_recommend_score,
                Review.public_review,
                Review.private_review,
                Review.created_at,
                Review.updated_at,
            ).where(Review.id == review_id, _yours(author))
        )
    ).mappings()
    found = row.one_or_none()
    return dict(found) if found is not None else None


def _yours(author: UUID) -> Any:
    """The author's own live reviews — the scope every read here shares.

    Withdrawn ones are absent: a review taken down by moderation is not the
    author's to read back, edit or find again.
    """
    return and_(Review.reviewed_by == author, Review.deleted_at.is_(None))


def _after(cursor: tuple[str, UUID]) -> Any:
    """The keyset position, as a comparison on ``(created_at, id)``.

    The same shape `session_store._after` uses, and for the same reason: the sort
    key is a timestamp rendered as text, so it is parsed back, and a token that
    survives base64 but holds something that is not a timestamp is a **client**
    error. Raising here rather than letting `fromisoformat` escape turns a 500
    into the 422 the envelope documents.
    """
    raw, after_id = cursor
    try:
        after = dt.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValidationError("cursor is not a cursor this endpoint issued") from exc
    # Descending, so the page moves *backwards* through time.
    return tuple_(Review.created_at, Review.id) < tuple_(literal(after), literal(after_id))


async def list_mentor_reviews(
    session: AsyncSession,
    mentor: UUID,
    *,
    limit: int,
    after: tuple[str, UUID] | None = None,
    session_type: UUID | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """One page of a mentor's published reviews, newest first.

    **Each review says which offering it was about**, reached through its
    session — `reviews` holds only the session, because a second key to the
    offering would be the same fact twice (the model's own note). Both joins are
    outer: the 53 migrated reviews have no session, and they stay listed with no
    topic rather than vanishing from a list whose length is `reviews.count`.
    The offering is read whatever its state now: retiring it does not change
    what an old session was about. `session_type` narrows the page to one
    offering, and the cursor still works within it because the sort key is
    unchanged.

    **Ordered on `created_at`, with the id breaking ties** — ADR 0016's amended
    form, *"the cursor is the sort column plus the id"*.

    **Not the id alone, though a UUIDv7 would make that tempting.** Id order is
    creation order only for rows this product wrote. The 53 migrated reviews take
    `uuid_generate_v7()` at *load* time while carrying `created_at` backfilled
    from Bubble, so an id-ordered list would put a 2023 review at the top of
    "newest first" with a three-year-old date rendered beside it. Sorting on the
    column the client actually displays cannot be inverted by when a row happened
    to be inserted.

    Fixed before the loader rather than after, because reordering a list somebody
    has already paged through is the expensive half.

    **The surname never leaves the database.** `left(last_name, 1)` is computed
    in SQL, so the column is not selected at all — a review is public and
    attributed, and "Fauziyah F." is the attribution the product chose. A read
    model dropping the surname would still have fetched it, and the next person
    to add a field would find it sitting there.

    The reviewer's institution comes from `top_qualification`, the same lateral
    the discovery card uses for a mentor's own degree, pointed at the author.
    Two copies of *which* institution represents somebody would drift, and the
    copy with fewer tests is the one that would.

    **A deleted reviewer's review is listed, unattributed** (`review_authors`),
    so this list and `published()`'s count are the same set of rows.

    Scoped by `published()`, so a withdrawn review is absent here exactly as it
    is absent from the averages — the one thing withdrawal is *for*.
    """
    statement = (
        # **This endpoint needs no token**, so a deleted reviewer's identity is
        # withheld by the join itself — see `review_authors`. Their review stays,
        # which is what keeps this list's length equal to the profile's count.
        with_author(
            select(
                Review.id,
                Review.created_at,
                Review.public_review,
                Review.overall_rating,
                review_value().label("session_value"),
                SessionType.id.label("session_type_id"),
                SessionType.name.label("session_type_name"),
                *author_columns(),
            ).select_from(Review)
        )
        .outerjoin(Session, Session.id == Review.session_id)
        .outerjoin(SessionType, SessionType.id == Session.session_type_id)
        .where(
            published(mentor),
            *([_after(after)] if after is not None else []),
            *([Session.session_type_id == session_type] if session_type is not None else []),
        )
        .order_by(Review.created_at.desc(), Review.id.desc())
        .limit(limit + 1)
    )
    rows = [dict(row) for row in (await session.execute(statement)).mappings()]
    return rows[:limit], len(rows) > limit


async def list_authored_reviews(
    session: AsyncSession,
    author: UUID,
    *,
    limit: int,
    after: tuple[str, UUID] | None = None,
    mentor: UUID | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """One page of the reviews ``author`` wrote, newest first.

    What "find my review of this mentor after a reload" needs, so ``mentor``
    narrows it to one subject. Scoped by `_yours()`, the predicate
    `get_review_row` uses, and ordered and paged like `list_mentor_reviews`.
    ``private_review`` is not selected: the single review's own read carries it.
    """
    statement = (
        select(
            Review.id,
            Review.created_at,
            Review.session_id,
            Review.reviewed_for,
            Review.overall_rating,
            Review.public_review,
        )
        .where(
            _yours(author),
            *([Review.reviewed_for == mentor] if mentor is not None else []),
            *([_after(after)] if after is not None else []),
        )
        .order_by(Review.created_at.desc(), Review.id.desc())
        .limit(limit + 1)
    )
    rows = [dict(row) for row in (await session.execute(statement)).mappings()]
    return rows[:limit], len(rows) > limit
