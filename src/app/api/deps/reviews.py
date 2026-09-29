"""Writing, reading, reporting and moderating reviews."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps.core import (
    CatalogueAdminDep,
    CurrentUserDep,
    OptionalViewerDep,
    QueueViewerDep,
    SessionDep,
    _configured,
)
from app.api.schemas.common import (
    MAX_PAGE_SIZE,
    clamp_limit,
    decode_cursor,
    encode_cursor,
)
from app.api.schemas.review_reports import ReportDecisionWrite, ReviewReportWrite
from app.api.schemas.reviews import ReviewEdit, ReviewWrite
from app.core.errors import (
    NotFoundError,
)
from app.domain.reviews import edit_window, editable_until
from app.infra.db.mentor_public_store import (
    get_public_mentor_id,
)
from app.infra.db.mentor_relationship import mentor_relationship
from app.infra.db.own_review_reader import list_reviews_about
from app.infra.db.review_eligibility import reviewable_sessions
from app.infra.db.review_moderation import decide_report, list_reviews_for_moderation
from app.infra.db.review_reader import (
    get_review_row,
    list_authored_reviews,
    list_mentor_reviews,
)
from app.infra.db.review_report_writer import report_review
from app.infra.db.review_writer import edit_review, write_review

# `get_session` is aliased: this module already has one, and it is the **database
# session** dependency at line 142. Two callables with that name in one file is a
# collision a reader resolves by scrolling, and the wrong one is a plausible
# mistake rather than an obvious error — `bubble_id` shadowed a local the same
# way in the M4 transform and raised `UnboundLocalError` far from the edit.

# --------------------------------------------------------------------------
# Reviews
#
# `now` is taken once per request and threaded through, rather than each layer
# calling the clock. Two reads of `datetime.now()` inside one write can land
# either side of a window's edge, and the test that pins the boundary needs a
# clock it can set.
# --------------------------------------------------------------------------


def _with_deadline(row: dict[str, Any], now: dt.datetime, window: dt.timedelta) -> dict[str, Any]:
    """An author's review row with `editable_until` stamped on.

    Every author read goes through here — the single review and the list — so
    the edge a client is shown comes from the same `editable_until` the `PATCH`
    guard asks, never a second computation of it.
    """
    return row | {"editable_until": editable_until(row["created_at"], now, window)}


async def _own_review(
    session: AsyncSession, review_id: UUID, author: UUID, now: dt.datetime, window: dt.timedelta
) -> dict[str, Any] | None:
    """The author's review with its deadline, or ``None``."""
    row = await get_review_row(session, review_id, author)
    return None if row is None else _with_deadline(row, now, window)


async def written_review(
    payload: ReviewWrite, user: CurrentUserDep, session: SessionDep, request: Request
) -> dict[str, Any]:
    """Write the review and read it back, in one transaction.

    Read back through a `SELECT` rather than assembled from the insert's own
    values, following `booked_session`: the response is the shape every other
    review read returns, built by the same code, so the two cannot drift.
    """
    now = dt.datetime.now(dt.UTC)
    review_id = await write_review(session, user["id"], payload.to_columns(), now=now)
    row = await _own_review(session, review_id, user["id"], now, edit_window(_configured(request)))
    if row is None:  # pragma: no cover - written in this transaction
        raise NotFoundError("no such review of yours")
    await session.commit()
    return row


async def edited_review(
    review_id: UUID,
    payload: ReviewEdit,
    user: CurrentUserDep,
    session: SessionDep,
    request: Request,
) -> dict[str, Any]:
    """Apply the edit and return the review as it now stands."""
    now = dt.datetime.now(dt.UTC)
    window = edit_window(_configured(request))
    await edit_review(session, user["id"], review_id, payload.to_columns(), now=now, window=window)
    row = await _own_review(session, review_id, user["id"], now, window)
    if row is None:  # pragma: no cover - `edit_review` already refused if absent
        raise NotFoundError("no such review of yours")
    await session.commit()
    return row


async def authored_review(
    review_id: UUID, user: CurrentUserDep, session: SessionDep, request: Request
) -> dict[str, Any]:
    """The caller's own review, whole — scoped to them in the query."""
    row = await _own_review(
        session, review_id, user["id"], dt.datetime.now(dt.UTC), edit_window(_configured(request))
    )
    if row is None:
        raise NotFoundError("no such review of yours")
    return row


#: `?mentor_id=` on the author's review lists — one declaration, so the two
#: endpoints that narrow to a mentor cannot describe or validate it differently.
MentorFilterQuery = Annotated[
    UUID | None,
    Query(description="Narrow to one mentor, which is what a profile tab wants."),
]


async def own_reviewable_sessions(
    user: CurrentUserDep,
    session: SessionDep,
    mentor_id: MentorFilterQuery = None,
    limit: Annotated[int | None, Query(ge=1, le=MAX_PAGE_SIZE)] = None,
) -> list[dict[str, Any]]:
    """What the caller may review right now, newest first."""
    result = await session.execute(
        reviewable_sessions(
            user["id"], dt.datetime.now(dt.UTC), mentor_id, limit=clamp_limit(limit)
        )
    )
    return [dict(row) for row in result.mappings()]


async def authored_reviews_page(
    user: CurrentUserDep,
    session: SessionDep,
    request: Request,
    mentor_id: MentorFilterQuery = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int | None, Query(ge=1, le=MAX_PAGE_SIZE)] = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """One page of the reviews the caller wrote, each with its deadline.

    The two-part codec, minted beside the decode as `own_reviews_page` does.
    """
    rows, has_more = await list_authored_reviews(
        session,
        user["id"],
        limit=clamp_limit(limit),
        after=decode_cursor(cursor),
        mentor=mentor_id,
    )
    now, window = dt.datetime.now(dt.UTC), edit_window(_configured(request))
    rows = [_with_deadline(row, now, window) for row in rows]
    if not (has_more and rows):
        return rows, None
    last = rows[-1]
    return rows, encode_cursor(last["created_at"].isoformat(), last["id"])


WrittenReviewDep = Annotated[dict[str, Any], Depends(written_review)]
AuthoredReviewsDep = Annotated[
    tuple[list[dict[str, Any]], str | None], Depends(authored_reviews_page)
]
EditedReviewDep = Annotated[dict[str, Any], Depends(edited_review)]
AuthoredReviewDep = Annotated[dict[str, Any], Depends(authored_review)]
ReviewableSessionsDep = Annotated[list[dict[str, Any]], Depends(own_reviewable_sessions)]


async def own_mentor_relationship(
    mentor_id: UUID, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    """The caller's history, as a mentee, with one mentor.

    No visibility check on the mentor, deliberately: the figures are the
    caller's own sessions and reviews, which they may always read, and an id
    that is nobody reads as no history rather than a `404` that would say which
    ids are mentors.
    """
    return await mentor_relationship(session, user["id"], mentor_id, now=dt.datetime.now(dt.UTC))


MentorRelationshipDep = Annotated[dict[str, Any], Depends(own_mentor_relationship)]


async def mentor_reviews_page(
    handle: str,
    session: SessionDep,
    viewer: OptionalViewerDep,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int | None, Query(ge=1, le=MAX_PAGE_SIZE)] = None,
    session_type: Annotated[
        UUID | None,
        Query(
            description=(
                "Only reviews of sessions booked as this offering — an id from a "
                "review's `session_type`, or from the profile's `session_types`. "
                "An id nothing was reviewed under is an empty page."
            )
        ),
    ] = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """One page of a mentor's published reviews.

    The handle resolves through the same predicate and the same visibility pair
    the profile uses, so a paused mentor's reviews are absent exactly as their
    profile is. `404` rather than an empty page: no such mentor is a different
    answer from a mentor with nothing to show, and a client renders them
    differently.
    """
    mentor = await get_public_mentor_id(session, handle, viewer)
    if mentor is None:
        raise NotFoundError("no such mentor")
    # The **two-part** codec, because the list sorts on `created_at` rather than
    # on the id. Mispairing the two forms is a paging bug that only shows on page
    # two, which this endpoint has already had once.
    rows, has_more = await list_mentor_reviews(
        session,
        mentor,
        limit=clamp_limit(limit),
        after=decode_cursor(cursor),
        session_type=session_type,
    )
    # **Minted here, beside the decode.** `mentor_page` states the rule: the token
    # is issued where the sort key is known, because deriving it again in the
    # route is one rule in two places — and the two halves drifting apart is
    # exactly the defect that made every cursor this endpoint issued invalid.
    if not (has_more and rows):
        return rows, None
    last = rows[-1]
    return rows, encode_cursor(last["created_at"].isoformat(), last["id"])


MentorReviewsDep = Annotated[tuple[list[dict[str, Any]], str | None], Depends(mentor_reviews_page)]


async def own_reviews_page(
    user: CurrentUserDep,
    session: SessionDep,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int | None, Query(ge=1, le=MAX_PAGE_SIZE)] = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """One page of the reviews written *about* the caller.

    No authorization argument and no handle: `CurrentUserDep` is the subject, so
    there is no target to check and nothing a caller could name that is not
    theirs.

    The same two-part codec the public list uses, minted here beside the decode
    for the reason `mentor_page` records — deriving the token again in the route
    is one rule in two places, and the two halves drifting is what once made
    every cursor that endpoint issued invalid.
    """
    rows, has_more = await list_reviews_about(
        session, user["id"], limit=clamp_limit(limit), after=decode_cursor(cursor)
    )
    if not (has_more and rows):
        return rows, None
    last = rows[-1]
    return rows, encode_cursor(last["created_at"].isoformat(), last["id"])


OwnReviewsDep = Annotated[tuple[list[dict[str, Any]], str | None], Depends(own_reviews_page)]


async def reported_review(
    review_id: UUID,
    payload: ReviewReportWrite,
    user: CurrentUserDep,
    session: SessionDep,
) -> Any:
    """File a report against a review of the caller.

    The composite key is the guarantee; `report_review` scopes the lookup so a
    review about somebody else is **404 rather than 403** — confirming it exists
    would turn an authorization answer into an enumeration oracle.
    """
    filed = await report_review(
        session,
        user["id"],
        review_id,
        reason=payload.reason,
        detail=payload.detail,
    )
    await session.commit()
    return filed


ReportedReviewDep = Annotated[Any, Depends(reported_review)]


async def moderation_queue_page(
    _: QueueViewerDep,
    session: SessionDep,
    reported: Annotated[bool, Query()] = False,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int | None, Query(ge=1, le=MAX_PAGE_SIZE)] = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """One page of reviews for a moderator.

    `QueueViewerDep` rather than the acting grant: every live admin may look,
    which is the split `pending_institution_rows` and the mentor queue already
    use. Deciding is narrower — see below.
    """
    rows, has_more = await list_reviews_for_moderation(
        session,
        limit=clamp_limit(limit),
        after=decode_cursor(cursor),
        reported_only=reported,
    )
    if not (has_more and rows):
        return rows, None
    last = rows[-1]
    return rows, encode_cursor(last["created_at"].isoformat(), last["id"])


ModerationQueueDep = Annotated[
    tuple[list[dict[str, Any]], str | None], Depends(moderation_queue_page)
]


async def decided_report(
    report_id: UUID,
    payload: ReportDecisionWrite,
    admin_id: CatalogueAdminDep,
    session: SessionDep,
) -> Any:
    """Rule on a report, and remove the review if it is upheld.

    **`CatalogueAdminDep` — super_admin only.** Every live grant may *look* at
    the queue; removing somebody's review from a public profile is the same
    weight as curating the catalogue, and `AdminRole` has no moderation grant to
    name. Adding one without anything to grant it would make the enum
    decorative, which settled decision #21 refuses.
    """
    decision = await decide_report(session, admin_id, report_id, payload.outcome)
    await session.commit()
    return decision


DecidedReportDep = Annotated[Any, Depends(decided_report)]
