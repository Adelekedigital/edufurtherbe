"""The default offering's form: written by the legacy load, and adopted once by
the offerings that load made before it had one (decision 367).

**"Never had a question" is the one test of untouched**, soft-deleted rows
included: a mentor who removed the questions chose an empty form, and putting
them back would undo that. The adoption also needs the old name and an empty
description; anything else is the mentor's own and is left alone.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.domain.intake import DEFAULT_FORM, DEFAULT_SESSION_TYPE_NAME
from app.domain.transform.sessions import GENERAL_MENTORSHIP
from app.infra.db.intake_store import insert_question
from app.infra.db.models.intake import SessionTypeQuestion
from app.infra.db.models.sessions import SessionType
from app.infra.db.session_type_store import NAME_INDEX


def _never_asked(session_type_id: Any) -> ColumnElement[bool]:
    return ~exists().where(SessionTypeQuestion.session_type_id == session_type_id)


async def give_default_form(
    executor: AsyncSession | AsyncConnection, session_type_id: UUID
) -> bool:
    """Write `DEFAULT_FORM` onto an offering that has never had a question.

    False, and nothing written, when it has had any. So a re-run, or a load over
    a form the mentor changed, adds nothing. ``created_by`` is null: no person
    wrote these questions.
    """
    unasked = await executor.execute(select(_never_asked(session_type_id)))
    if not unasked.scalar_one():
        return False
    for order, question in enumerate(DEFAULT_FORM):
        await insert_question(
            executor,
            session_type_id,
            created_by=None,
            payload={
                "question_text": question.question_text,
                "question_type": question.question_type,
                "is_required": True,
                "display_order": order,
                "options_source": question.options_source,
            },
        )
    return True


def _untouched() -> list[ColumnElement[bool]]:
    return [
        SessionType.deleted_at.is_(None),
        SessionType.name == GENERAL_MENTORSHIP,
        or_(SessionType.description.is_(None), func.btrim(SessionType.description) == ""),
        _never_asked(SessionType.id),
    ]


async def untouched_defaults(session: AsyncSession) -> list[UUID]:
    """The legacy load's offerings no mentor has changed, oldest first."""
    rows = await session.execute(
        select(SessionType.id).where(*_untouched()).order_by(SessionType.id)
    )
    return list(rows.scalars())


async def adopt_default_form(session: AsyncSession, session_type_id: UUID) -> str:
    """Rename one untouched legacy default and give it the form, or say why not:
    ``adopted``, ``name_taken`` or ``changed``. The caller commits.

    **Locked, then asked again in a fresh statement.** `FOR UPDATE` rechecks
    the row's own columns after a wait, but not the `NOT EXISTS` subquery. A
    mentor's question insert takes `FOR KEY SHARE` on this offering, which the
    lock blocks, so the only question the lock can miss is one committed while
    it waited, and the second check sees that. Duration and description are
    not touched.

    **The rename is attempted, not pre-checked**: a mentor who already holds
    the new name trips `ix_session_types_mentor_name` inside a savepoint,
    which reads as ``name_taken`` and leaves this offering as it was.
    """
    locked = await session.execute(
        select(SessionType.id)
        .where(SessionType.id == session_type_id, *_untouched())
        .with_for_update()
    )
    if locked.scalar_one_or_none() is None:
        return "changed"
    unasked = await session.execute(select(_never_asked(session_type_id)))
    if not unasked.scalar_one():
        return "changed"
    try:
        async with session.begin_nested():
            await session.execute(
                update(SessionType)
                .where(SessionType.id == session_type_id)
                .values(name=DEFAULT_SESSION_TYPE_NAME)
            )
    except IntegrityError as exc:
        if NAME_INDEX in str(exc.orig):
            return "name_taken"
        raise
    await give_default_form(session, session_type_id)
    return "adopted"
