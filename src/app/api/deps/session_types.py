"""A mentor's session types, their windows and their intake questions."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps.core import (
    CREATED,
    ENDPOINT_QUESTION,
    ENDPOINT_SESSION_TYPE,
    BookingWindowDep,
    CurrentUserDep,
    IdempotencyKeyHeader,
    SessionDep,
    claim_idempotency_key,
    refuse_window_over_max,
)
from app.api.schemas.availability import (
    AvailabilityRulePatch,
    AvailabilityRuleWrite,
)
from app.api.schemas.intake import QuestionOrderWrite, QuestionPatch, QuestionWrite
from app.api.schemas.session_types import MentorSessionTypePatch, MentorSessionTypeWrite
from app.core.errors import (
    NotFoundError,
)
from app.infra.db.idempotency import Replayed, record_response
from app.infra.db.intake_store import (
    create_question,
    delete_question,
    list_questions,
    questions_by_type,
    reorder_questions,
    update_question,
)

# `get_session` is aliased: this module already has one, and it is the **database
# session** dependency at line 142. Two callables with that name in one file is a
# collision a reader resolves by scrolling, and the wrong one is a plausible
# mistake rather than an obvious error — `bubble_id` shadowed a local the same
# way in the M4 transform and raised `UnboundLocalError` far from the edit.
from app.infra.db.session_type_store import (
    DeletionScheduled,
    create_session_type,
    delete_session_type,
    get_own_session_type,
    list_own_session_types,
    list_session_types,
    restore_session_type,
    update_session_type,
)
from app.infra.db.session_type_window_store import (
    create_window,
    delete_window,
    list_windows,
    update_window,
)


async def own_session_type_windows(
    session_type_id: UUID, user: CurrentUserDep, session: SessionDep
) -> list[dict[str, Any]]:
    """An offering's own weekly windows; 404 when it is not the caller's (#199)."""
    windows = await list_windows(session, user["id"], session_type_id)
    if windows is None:
        raise NotFoundError("no such session type")
    return windows


async def created_session_type_window(
    session_type_id: UUID, payload: AvailabilityRuleWrite, user: CurrentUserDep, session: SessionDep
) -> UUID:
    window_id = await create_window(session, user["id"], session_type_id, payload.model_dump())
    if window_id is None:
        raise NotFoundError("no such session type")
    await session.commit()
    return window_id


async def updated_session_type_window(
    session_type_id: UUID,
    window_id: UUID,
    payload: AvailabilityRulePatch,
    user: CurrentUserDep,
    session: SessionDep,
) -> bool:
    changed = await update_window(
        session, user["id"], session_type_id, window_id, payload.model_dump(exclude_unset=True)
    )
    await session.commit()
    return changed


async def deleted_session_type_window(
    session_type_id: UUID, window_id: UUID, user: CurrentUserDep, session: SessionDep
) -> bool:
    removed = await delete_window(session, user["id"], session_type_id, window_id)
    await session.commit()
    return removed


SessionTypeWindowsDep = Annotated[list[dict[str, Any]], Depends(own_session_type_windows)]
CreatedSessionTypeWindowDep = Annotated[UUID, Depends(created_session_type_window)]
UpdatedSessionTypeWindowDep = Annotated[bool, Depends(updated_session_type_window)]
DeletedSessionTypeWindowDep = Annotated[bool, Depends(deleted_session_type_window)]


async def mentor_session_types(
    user_id: UUID, session: SessionDep, window: BookingWindowDep
) -> list[dict[str, Any]]:
    """What a mentor offers, or a 404 that does not say which kind of 404 it is.

    No `CurrentUserDep`, and that absence is the whole authorization decision —
    the mentor's own state stands in for a viewer, checked inside the query.

    `None` from the store means the mentor is not publicly visible. An **empty
    list** means they are, and are offering nothing bookable — a different claim,
    and one that must not be used to answer the first.
    """
    rows = await list_session_types(session, user_id, window=window)
    if rows is None:
        raise NotFoundError("no such mentor")
    return await _with_forms(session, rows)


async def _with_forms(
    session: AsyncSession, rows: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """Public session types with their intake forms attached (#207).

    One place, for both public reads — `/users/{id}/session-types` and the
    profile's inlined list — so the two cannot disagree about what a mentee is
    asked. Two queries for the whole page, whatever its size.
    """
    rows = rows or []
    forms = await questions_by_type(session, [row["id"] for row in rows])
    for row in rows:
        row["questions"] = forms.get(row["id"], [])
    return rows


SessionTypesDep = Annotated[list[dict[str, Any]], Depends(mentor_session_types)]


async def own_session_types(
    user: CurrentUserDep, session: SessionDep, window: BookingWindowDep
) -> list[dict[str, Any]]:
    """The caller's own offerings, including the ones they have switched off.

    **No authorization argument, and no `TargetUserDep`.** `CurrentUserDep` *is*
    the caller, so there is no target to resolve and nothing to admit an admin
    through — the same shape as `own_attributes` above. The scope is the caller's
    id spread into the store's `WHERE`, which is the only guard on this read.

    `user["id"]` rather than an attribute: `get_current_user` returns a plain
    `dict[str, Any]` built from a `text()` row, so nothing here is a typed model
    and a wrong key would be a `KeyError` at runtime rather than a validation
    error. The key is `SELECT`ed unconditionally by `CURRENT_USER`, so it is
    present whenever this runs — the same assumption every other dependency in
    this module already makes.
    """
    return await list_own_session_types(session, user["id"], window=window)


OwnSessionTypesDep = Annotated[list[dict[str, Any]], Depends(own_session_types)]


async def created_own_session_type(
    payload: MentorSessionTypeWrite,
    user: CurrentUserDep,
    session: SessionDep,
    window: BookingWindowDep,
    idempotency_key: Annotated[str | None, IdempotencyKeyHeader] = None,
) -> tuple[dict[str, Any], int, bool]:
    """The offering, its booking config and its questions, in one transaction.

    **Questions ride in the same transaction** (#196): each goes through
    `create_question`, the same writer `POST .../questions` uses, so the limit
    and the type rules are one rule; one refused refuses the lot.

    **`Idempotency-Key` is optional here** (#196): sent, a retry replays the first
    answer; absent, the request behaves as it always has. Returns the body, the
    status and whether it was a replay.

    **One commit, after both inserts.** `/slots` and both read paths inner-join
    `session_type_booking_configs`, so an offering without one is invisible
    everywhere and unbookable, and nothing writes a config on its own — a commit
    between the two statements would make that state reachable and permanent.

    `CurrentUserDep` rather than `OwnerDep`: there is no `{user_id}` in the path
    to resolve, so the caller *is* the scope, matching `own_session_types` above.
    """
    reservation = (
        await claim_idempotency_key(
            session,
            key=idempotency_key,
            user_id=user["id"],
            endpoint=ENDPOINT_SESSION_TYPE,
            body=payload.model_dump(mode="json"),
        )
        if idempotency_key is not None
        else None
    )
    if isinstance(reservation, Replayed):
        return reservation.body, reservation.status_code, True
    # After the replay lookup: a retry of a create that already succeeded gets
    # its stored answer, whatever the configured maximum became since.
    refuse_window_over_max(payload.booking_window_days, window)

    session_type_id = await create_session_type(session, user["id"], payload.model_dump())
    if session_type_id is None:
        raise NotFoundError("this user has no mentor profile")
    question_ids = []
    for question in payload.questions:
        question_id = await create_question(
            session, user["id"], session_type_id, question.model_dump()
        )
        if question_id is None:  # pragma: no cover - the offering was just written here
            raise NotFoundError("no such session type")
        question_ids.append(str(question_id))
    body = {"id": str(session_type_id), "question_ids": question_ids}
    if reservation is not None:
        await record_response(session, reservation, status_code=CREATED, body=body)
    await session.commit()
    return body, CREATED, False


async def updated_own_session_type(
    session_type_id: UUID,
    payload: MentorSessionTypePatch,
    user: CurrentUserDep,
    session: SessionDep,
    window: BookingWindowDep,
) -> bool:
    """`exclude_unset` is what makes this a PATCH: a field the client did not send
    is absent, not null. Without it every omitted field is written as its default
    and a one-field edit blanks the rest."""
    refuse_window_over_max(payload.booking_window_days, window)
    changed = await update_session_type(
        session, user["id"], session_type_id, payload.model_dump(exclude_unset=True)
    )
    await session.commit()
    return changed


async def deleted_own_session_type(
    session_type_id: UUID, user: CurrentUserDep, session: SessionDep
) -> bool | DeletionScheduled:
    """Delete now (`True`), schedule behind booked sessions (a `DeletionScheduled`),
    or `False` for not yours or already gone (#218)."""
    removed = await delete_session_type(session, user["id"], session_type_id)
    await session.commit()
    return removed


async def restored_own_session_type(
    session_type_id: UUID, user: CurrentUserDep, session: SessionDep, window: BookingWindowDep
) -> dict[str, Any]:
    """Cancel a scheduled deletion and answer the offering as the list shows it."""
    if not await restore_session_type(session, user["id"], session_type_id):
        raise NotFoundError("no such session type")
    await session.commit()
    row = await get_own_session_type(session, user["id"], session_type_id, window=window)
    if row is None:  # pragma: no cover - found and restored in this request
        raise NotFoundError("no such session type")
    return row


CreatedOwnSessionTypeDep = Annotated[
    tuple[dict[str, Any], int, bool], Depends(created_own_session_type)
]
UpdatedOwnSessionTypeDep = Annotated[bool, Depends(updated_own_session_type)]
DeletedOwnSessionTypeDep = Annotated[bool | DeletionScheduled, Depends(deleted_own_session_type)]
RestoredOwnSessionTypeDep = Annotated[dict[str, Any], Depends(restored_own_session_type)]


async def own_questions(
    session_type_id: UUID, user: CurrentUserDep, session: SessionDep
) -> list[dict[str, Any]]:
    """This offering's live questions, or a 404 that says nothing about why.

    `None` from the store means the offering is not the caller's — or does not
    exist, or is deleted. Indistinguishable on purpose: telling them apart says
    which ids exist.
    """
    rows = await list_questions(session, user["id"], session_type_id)
    if rows is None:
        raise NotFoundError("no such session type")
    return rows


async def created_own_question(
    session_type_id: UUID,
    payload: QuestionWrite,
    user: CurrentUserDep,
    session: SessionDep,
    idempotency_key: Annotated[str | None, IdempotencyKeyHeader] = None,
) -> tuple[dict[str, Any], int, bool]:
    """One question; with an `Idempotency-Key`, once however often it is sent.

    **The offering is part of the fingerprint**, so the same key and body sent
    to a different offering is a different request (a `422`), never a replay of
    a question on the wrong form.
    """
    reservation = (
        await claim_idempotency_key(
            session,
            key=idempotency_key,
            user_id=user["id"],
            endpoint=ENDPOINT_QUESTION,
            body={"session_type_id": str(session_type_id), **payload.model_dump(mode="json")},
        )
        if idempotency_key is not None
        else None
    )
    if isinstance(reservation, Replayed):
        return reservation.body, reservation.status_code, True

    question_id = await create_question(session, user["id"], session_type_id, payload.model_dump())
    if question_id is None:
        raise NotFoundError("no such session type")
    body = {"id": str(question_id)}
    if reservation is not None:
        await record_response(session, reservation, status_code=CREATED, body=body)
    await session.commit()
    return body, CREATED, False


async def updated_own_question(
    session_type_id: UUID,
    question_id: UUID,
    payload: QuestionPatch,
    user: CurrentUserDep,
    session: SessionDep,
) -> bool:
    changed = await update_question(
        session,
        user["id"],
        session_type_id,
        question_id,
        payload.model_dump(exclude_unset=True),
    )
    await session.commit()
    return changed


async def deleted_own_question(
    session_type_id: UUID, question_id: UUID, user: CurrentUserDep, session: SessionDep
) -> bool:
    removed = await delete_question(session, user["id"], session_type_id, question_id)
    await session.commit()
    return removed


async def reordered_own_questions(
    session_type_id: UUID, payload: QuestionOrderWrite, user: CurrentUserDep, session: SessionDep
) -> bool:
    """The whole form renumbered in one transaction (#197)."""
    reordered = await reorder_questions(session, user["id"], session_type_id, payload.question_ids)
    await session.commit()
    return reordered


OwnQuestionsDep = Annotated[list[dict[str, Any]], Depends(own_questions)]
CreatedOwnQuestionDep = Annotated[tuple[dict[str, Any], int, bool], Depends(created_own_question)]
UpdatedOwnQuestionDep = Annotated[bool, Depends(updated_own_question)]
DeletedOwnQuestionDep = Annotated[bool, Depends(deleted_own_question)]
ReorderedOwnQuestionsDep = Annotated[bool, Depends(reordered_own_questions)]
