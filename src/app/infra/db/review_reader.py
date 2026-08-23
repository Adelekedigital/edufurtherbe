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

from sqlalchemy import and_, func, literal, select, true, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationError
from app.infra.db.models.reviews import Review
from app.infra.db.models.user import User
from app.infra.db.predicates import LIVE
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
) -> tuple[list[dict[str, Any]], bool]:
    """One page of a mentor's published reviews, newest first.

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
            # `nullif`, because `left('', 1)` is `''` rather than null and a client
            # concatenating renders "Fauziyah .". Both name columns are nullable,
            # and the migrated rows do not go through the boundary that turns an
            # emptied string into null.
            func.nullif(func.left(User.last_name, 1), "").label("author_last_initial"),
            author.c.institution.label("author_institution"),
        )
        .select_from(Review)
        # **`LIVE`, and this endpoint needs no token.** A reviewer who deletes
        # their account must stop being named on a public page; without it their
        # first name, initial and institution stay published for good.
        # `predicates.LIVE` exists because this rule has been missed twice
        # already, and `test_predicates` walks only two stores, so nothing here
        # would have caught a third.
        .join(User, and_(User.id == Review.reviewed_by, LIVE))
        .outerjoin(author, true())
        .where(published(mentor), *([_after(after)] if after is not None else []))
        .order_by(Review.created_at.desc(), Review.id.desc())
        .limit(limit + 1)
    )
    rows = [dict(row) for row in (await session.execute(statement)).mappings()]
    return rows[:limit], len(rows) > limit
