"""Writing a review, and the two windows that decide whether it may be written.

**`reviewed_for` never comes from the caller.** It is read from the session's
own `mentor_id` inside this transaction, which is what enforces the one
invariant `reviews` cannot express as a constraint: *`reviewed_for` equals
`sessions.mentor_id` whenever `session_id` is present*. A `CHECK` cannot span
two tables, so the rule lives at the single write path — the same honest split
`session_stats` documents for `sessions.status` against
`session_participants.attendance_status`. Taking it from the body instead would
let a caller aim a review at somebody who never mentored them.

**What `PATCH` may change is decided by `ReviewEdit`, not here.** A second
list of editable columns in this module would be the same rule in two places
(#8) — and the one that drifts is always the copy the tests do not go
through. `session_id` is absent from that model because it is *identity*:
moving a review to another session would step around every eligibility clause
rather than fix a typo.

**Authorization is in the `WHERE`, on both paths.** The session is fetched
scoped to the caller as its mentee and the review is fetched scoped to the
caller as its author, so a row that is not theirs is *absent* rather than
fetched-then-refused. Non-negotiable #5, and the reason the refusal is `404`:
`403` would confirm the row exists.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    AlreadyReviewedError,
    ConflictError,
    NotFoundError,
    ReviewIntervalError,
)
from app.domain.enums import SessionStatus
from app.domain.reviews import edit_window_open
from app.infra.db.models.reviews import Review
from app.infra.db.models.sessions import Session
from app.infra.db.review_eligibility import already_reviewed, within_interval

__all__ = ["edit_review", "write_review"]


async def write_review(
    session: AsyncSession, author: UUID, values: dict[str, Any], now: dt.datetime
) -> UUID:
    """Insert one review of one completed session, or say why not.

    Raises :class:`NotFoundError` when the session does not exist, is not the
    caller's, or has not completed — all one answer, because distinguishing them
    tells anyone who can enumerate ids which sessions are real.

    :class:`AlreadyReviewedError` and :class:`ReviewIntervalError` are both
    `409`s and both carry a problem type, because a client responds to them
    differently: the first is terminal, the second resolves when the window
    passes.

    **Already-reviewed is checked first, and the order is load-bearing.** A
    review of this session is itself a review of this offering, so once one
    exists *both* refusals are true — and reporting the retryable one would tell
    a client to come back in a month for something that will never succeed.
    """
    session_id = values["session_id"]
    target = (
        await session.execute(
            select(Session.id, Session.mentor_id, Session.session_type_id).where(
                Session.id == session_id,
                Session.mentee_id == author,
                Session.status == SessionStatus.COMPLETED,
            )
        )
    ).one_or_none()
    if target is None:
        raise NotFoundError("no such completed session of yours")

    if await session.scalar(select(already_reviewed(session_id, author))):
        raise AlreadyReviewedError("you have already reviewed this session")

    if await session.scalar(select(within_interval(author, target.session_type_id, now))):
        raise ReviewIntervalError(
            "you reviewed this offering recently; a further review is not open yet"
        )

    written = await session.execute(
        insert(Review)
        .values(reviewed_by=author, reviewed_for=target.mentor_id, **values)
        .returning(Review.id)
    )
    return UUID(str(written.scalar_one()))


async def edit_review(
    session: AsyncSession,
    author: UUID,
    review_id: UUID,
    changes: dict[str, Any],
    now: dt.datetime,
) -> None:
    """Apply a compose-grace-period edit, or refuse.

    Raises :class:`NotFoundError` when the review is not the caller's or has been
    withdrawn, and :class:`ConflictError` once the window has shut.

    **Withdrawn reviews are invisible here**, which is the one place a soft
    delete changes an answer on this path: a review taken down by moderation is
    not editable back into existence. The eligibility clauses take the opposite
    view on purpose — they still count it, because it still happened.

    **The window is measured from `created_at`.** `updated_at` moves on every
    edit, so reading it would let a review be rewritten indefinitely, ten
    minutes at a time.
    """
    existing = (
        await session.execute(
            select(Review.id, Review.created_at).where(
                Review.id == review_id,
                Review.reviewed_by == author,
                Review.deleted_at.is_(None),
            )
        )
    ).one_or_none()
    if existing is None:
        raise NotFoundError("no such review of yours")

    if not edit_window_open(existing.created_at, now):
        raise ConflictError("the edit window for this review has closed")

    if not changes:
        return

    await session.execute(update(Review).where(Review.id == review_id).values(**changes))
