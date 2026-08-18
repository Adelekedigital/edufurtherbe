"""A mentor's intake questions — the form their offering asks before a session.

**Every statement is scoped through `session_type_of()`**, which is ownership
plus soft deletion and deliberately not `is_active`: editing the form of a paused
offering is the ordinary case, and is what preparing it to be switched back on
looks like. That is the same predicate the offering writes use, reused rather
than retyped (#8).

**A question is never hard-deleted.** `intake_answers.question_id` restricts, so
a question somebody has answered cannot go — and `deleted_at` is what lets a
mentor edit their form without being refused by every answer ever given to it.
The row stays; the reads stop returning it.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, Select, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.domain.intake import MAX_QUESTIONS
from app.infra.db.models.intake import SessionTypeQuestion
from app.infra.db.models.sessions import SessionType
from app.infra.db.public_visibility import session_type_of

__all__ = [
    "create_question",
    "delete_question",
    "list_questions",
    "update_question",
]

#: The live form, in the order a mentee sees it. `id` breaks a tie on
#: `display_order`, which has a server default of `0` — so a mentor who never
#: sets it still gets a total order rather than one that shifts between requests.
QUESTION_COLUMNS = (
    SessionTypeQuestion.id,
    SessionTypeQuestion.question_text,
    SessionTypeQuestion.question_type,
    SessionTypeQuestion.is_required,
    SessionTypeQuestion.display_order,
)


def _live_questions(session_type_id: UUID) -> Select[Any]:
    return (
        select(*QUESTION_COLUMNS)
        .where(
            SessionTypeQuestion.session_type_id == session_type_id,
            SessionTypeQuestion.deleted_at.is_(None),
        )
        .order_by(SessionTypeQuestion.display_order, SessionTypeQuestion.id)
    )


async def _owns(session: AsyncSession, mentor_user_id: UUID, session_type_id: UUID) -> bool:
    """Whether this offering is the caller's, and still exists.

    Asked as its own statement rather than folded into each write, because the
    answer decides between `404` and everything else — and a write that found no
    row could not tell "not yours" from "already at five".
    """
    scoped = select(SessionType.id).where(
        *session_type_of(mentor_user_id), SessionType.id == session_type_id
    )
    return (await session.execute(scoped)).first() is not None


async def list_questions(
    session: AsyncSession, mentor_user_id: UUID, session_type_id: UUID
) -> list[dict[str, Any]] | None:
    """This offering's live questions, or ``None`` if it is not the caller's.

    ``None`` rather than an empty list, because the two are different statements:
    *this offering is not yours* and *your offering asks nothing yet*. Collapsing
    them would answer `200` for another mentor's id and tell the caller their
    form is empty.
    """
    if not await _owns(session, mentor_user_id, session_type_id):
        return None
    result = await session.execute(_live_questions(session_type_id))
    return [dict(row) for row in result.mappings()]


async def create_question(
    session: AsyncSession, mentor_user_id: UUID, session_type_id: UUID, payload: dict[str, Any]
) -> UUID | None:
    """Add one question. ``None`` if the offering is not the caller's.

    **The five-question limit is counted here, and it races.** Two concurrent
    creates can both see four and both insert, leaving six. No constraint can
    express a per-group cardinality, and the alternative is a counting trigger —
    which is the mechanism `#107` removed from this schema and which would be
    reintroduced for a loser that leaves one extra question on a form rather than
    corrupting anything. Named rather than hidden, on the same terms as the
    booked-offering check in `delete_session_type`.

    **The count is of *live* questions**, so deleting one frees a slot. Counting
    every row would leave a mentor who added and removed five stuck forever,
    with nothing on their form to explain it.
    """
    if not await _owns(session, mentor_user_id, session_type_id):
        return None

    live = await session.execute(
        select(func.count())
        .select_from(SessionTypeQuestion)
        .where(
            SessionTypeQuestion.session_type_id == session_type_id,
            SessionTypeQuestion.deleted_at.is_(None),
        )
    )
    if live.scalar_one() >= MAX_QUESTIONS:
        raise ConflictError(
            f"a session type may ask at most {MAX_QUESTIONS} questions; "
            "delete one before adding another"
        )

    return (
        await session.execute(
            insert(SessionTypeQuestion)
            .values(
                session_type_id=session_type_id,
                created_by=mentor_user_id,
                question_text=payload["question_text"],
                question_type=payload["question_type"],
                is_required=payload["is_required"],
                display_order=payload["display_order"],
            )
            .returning(SessionTypeQuestion.id)
        )
    ).scalar_one()


async def update_question(
    session: AsyncSession,
    mentor_user_id: UUID,
    session_type_id: UUID,
    question_id: UUID,
    payload: dict[str, Any],
) -> bool:
    """Change one question. ``False`` if it is not on the caller's offering.

    **Both ids are in the `WHERE`.** Scoping on `question_id` alone would let any
    mentor edit any question by guessing an id — the offering is what carries
    ownership, so the question must be reached through it rather than looked up
    and checked afterwards (non-negotiable #5).
    """
    if not await _owns(session, mentor_user_id, session_type_id):
        return False
    if not payload:
        return True

    result = await session.execute(
        update(SessionTypeQuestion)
        .where(
            SessionTypeQuestion.id == question_id,
            SessionTypeQuestion.session_type_id == session_type_id,
            SessionTypeQuestion.deleted_at.is_(None),
        )
        .values(**payload)
    )
    return cast("CursorResult[Any]", result).rowcount > 0


async def delete_question(
    session: AsyncSession, mentor_user_id: UUID, session_type_id: UUID, question_id: UUID
) -> bool:
    """Retire one question. ``False`` if it is not on the caller's offering.

    **Soft, and that is the whole reason `session_type_questions` carries
    `deleted_at`.** `intake_answers.question_id` restricts, so a hard delete of
    an answered question is refused by the database — and a mentor whose form can
    never change once anybody has filled it in is not a form. The row stays,
    every answer keeps the question it answered, and the live reads drop it.
    """
    if not await _owns(session, mentor_user_id, session_type_id):
        return False

    result = await session.execute(
        update(SessionTypeQuestion)
        .where(
            SessionTypeQuestion.id == question_id,
            SessionTypeQuestion.session_type_id == session_type_id,
            SessionTypeQuestion.deleted_at.is_(None),
        )
        .values(deleted_at=func.now())
    )
    return cast("CursorResult[Any]", result).rowcount > 0
