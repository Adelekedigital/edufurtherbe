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

from sqlalchemy import CursorResult, Select, delete, exists, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ValidationError
from app.domain.enums import IntakeStatus, QuestionType
from app.domain.intake import MAX_QUESTIONS
from app.infra.db.models.intake import (
    IntakeAnswer,
    IntakeSubmission,
    SessionTypeQuestion,
    SessionTypeQuestionOption,
)
from app.infra.db.models.sessions import SessionType
from app.infra.db.public_visibility import session_type_of

__all__ = [
    "create_question",
    "delete_question",
    "list_questions",
    "live_question",
    "live_question_count",
    "questions_by_type",
    "record_answers",
    "reorder_questions",
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
    SessionTypeQuestion.allows_multiple,
)


def live_question() -> list[Any]:
    """A question still on its offering's form: not deleted.

    The one definition. The form loader, the five-question cap and the owner's
    `question_count` all read it, so the count can never disagree with the list
    `GET /me/session-types/{id}/questions` returns (frontend #147).
    """
    return [SessionTypeQuestion.deleted_at.is_(None)]


def live_question_count(session_type_id: Any) -> Any:
    """Scalar subquery: how many questions this offering's form asks."""
    return (
        select(func.count(SessionTypeQuestion.id))
        .where(SessionTypeQuestion.session_type_id == session_type_id, *live_question())
        .scalar_subquery()
    )


def _live_questions(session_type_ids: list[UUID]) -> Select[Any]:
    return (
        select(SessionTypeQuestion.session_type_id, *QUESTION_COLUMNS)
        .where(SessionTypeQuestion.session_type_id.in_(session_type_ids), *live_question())
        .order_by(SessionTypeQuestion.display_order, SessionTypeQuestion.id)
    )


async def questions_by_type(
    session: AsyncSession, session_type_ids: list[UUID]
) -> dict[UUID, list[dict[str, Any]]]:
    """Each offering's live form, options included — two queries for any number.

    **Unscoped by owner, deliberately**: the owner's list checks ownership
    before calling this, and the public reads call it only for offerings they
    already found visible. It is the one loader of a form, so the owner's view,
    the public view and the booking check cannot disagree about what is asked.
    """
    if not session_type_ids:
        return {}
    result = await session.execute(_live_questions(session_type_ids))
    questions = [dict(row) for row in result.mappings()]
    options = await _options_of(session, [q["id"] for q in questions])
    grouped: dict[UUID, list[dict[str, Any]]] = {}
    for question in questions:
        question["options"] = options.get(question["id"], [])
        grouped.setdefault(question.pop("session_type_id"), []).append(question)
    return grouped


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
    return (await questions_by_type(session, [session_type_id])).get(session_type_id, [])


async def _options_of(
    session: AsyncSession, question_ids: list[UUID]
) -> dict[UUID, list[dict[str, Any]]]:
    """Each question's options in order, for several questions in one query."""
    if not question_ids:
        return {}
    rows = await session.execute(
        select(
            SessionTypeQuestionOption.question_id,
            SessionTypeQuestionOption.id,
            SessionTypeQuestionOption.option_text.label("text"),
        )
        .where(SessionTypeQuestionOption.question_id.in_(question_ids))
        .order_by(SessionTypeQuestionOption.sort_order, SessionTypeQuestionOption.id)
    )
    grouped: dict[UUID, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row.question_id, []).append({"id": row.id, "text": row.text})
    return grouped


async def _insert_options(
    session: AsyncSession, question_id: UUID, texts: list[str], start: int = 0
) -> None:
    for position, option_text in enumerate(texts, start=start):
        await session.execute(
            insert(SessionTypeQuestionOption).values(
                question_id=question_id, option_text=option_text, sort_order=position
            )
        )


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

    live = await session.execute(select(live_question_count(session_type_id)))
    if live.scalar_one() >= MAX_QUESTIONS:
        raise ConflictError(
            f"a session type may ask at most {MAX_QUESTIONS} questions; "
            "delete one before adding another"
        )

    question_id: UUID = (
        await session.execute(
            insert(SessionTypeQuestion)
            .values(
                session_type_id=session_type_id,
                created_by=mentor_user_id,
                question_text=payload["question_text"],
                question_type=payload["question_type"],
                is_required=payload["is_required"],
                display_order=payload["display_order"],
                allows_multiple=payload.get("allows_multiple", False),
            )
            .returning(SessionTypeQuestion.id)
        )
    ).scalar_one()
    # The boundary has already refused options on anything but `multi_choice`.
    await _insert_options(session, question_id, [o["text"] for o in payload.get("options") or []])
    return question_id


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
    current = (
        await session.execute(
            select(SessionTypeQuestion.question_type).where(
                SessionTypeQuestion.id == question_id,
                SessionTypeQuestion.session_type_id == session_type_id,
                *live_question(),
            )
        )
    ).scalar_one_or_none()
    if current is None:
        return False
    if not payload:
        return True

    options = payload.pop("options", None)
    was_choice = current == QuestionType.MULTI_CHOICE
    is_choice = payload.get("question_type", current) == QuestionType.MULTI_CHOICE
    # **Not across the choice line, either way** (#200): a choice answer is an
    # option id and a free-text one is prose, and neither means anything as the
    # other. Delete the question and add a new one instead.
    if was_choice != is_choice:
        raise ValidationError(
            "a question cannot change to or from multi_choice; delete it and add a new one"
        )
    if not is_choice and (options is not None or payload.get("allows_multiple")):
        raise ValidationError("options and allows_multiple are only for multi_choice questions")

    if payload:
        await session.execute(
            update(SessionTypeQuestion)
            .where(
                SessionTypeQuestion.id == question_id,
                SessionTypeQuestion.session_type_id == session_type_id,
            )
            .values(**payload)
        )
    if options is not None:
        await _replace_options(session, question_id, options)
    return True


async def _replace_options(
    session: AsyncSession, question_id: UUID, options: list[dict[str, Any]]
) -> None:
    """Make the question's options exactly `options`, in that order.

    **An `id` keeps that option** — its text and position may change, and every
    answer that chose it still means it. **No `id` adds one. One left out goes**,
    unless an answer chose it: `intake_answers.selected_option_id` restricts, so
    that is a `409` naming the reason rather than a constraint error, and nothing
    is changed. An `id` that is not one of *this* question's options is a `422`.
    """
    existing = {
        row.id
        for row in await session.execute(
            select(SessionTypeQuestionOption.id).where(
                SessionTypeQuestionOption.question_id == question_id
            )
        )
    }
    kept = [o["id"] for o in options if o.get("id") is not None]
    if not set(kept) <= existing:
        raise ValidationError("an option id is not one of this question's options")
    removed = existing - set(kept)
    if removed:
        answered = (
            await session.execute(
                select(exists().where(IntakeAnswer.selected_option_id.in_(removed)))
            )
        ).scalar_one()
        if answered:
            raise ConflictError(
                "an option a mentee has already chosen cannot be removed; keep it, or rename it"
            )
        await session.execute(
            delete(SessionTypeQuestionOption).where(SessionTypeQuestionOption.id.in_(removed))
        )
    for position, option in enumerate(options):
        if option.get("id") is not None:
            await session.execute(
                update(SessionTypeQuestionOption)
                .where(SessionTypeQuestionOption.id == option["id"])
                .values(option_text=option["text"], sort_order=position)
            )
        else:
            await _insert_options(session, question_id, [option["text"]], start=position)


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
            *live_question(),
        )
        .values(deleted_at=func.now())
    )
    return cast("CursorResult[Any]", result).rowcount > 0


async def reorder_questions(
    session: AsyncSession, mentor_user_id: UUID, session_type_id: UUID, question_ids: list[UUID]
) -> bool:
    """Put this offering's form in the order given. ``False`` if it is not the caller's.

    **The list must be exactly the live form** — every live question once, and
    nothing else. A missing id would leave a question at a stale position among
    renumbered ones; an extra one is another offering's or a deleted question;
    a repeat has no single position. Each is a `ValidationError` naming what is
    wrong, and nothing is written. The offering is reached through its owner
    (non-negotiable #5), so another mentor's is a `False` — a `404` — before any
    question is looked at.

    Renumbered from zero in the order given, in the caller's transaction.
    """
    if not await _owns(session, mentor_user_id, session_type_id):
        return False
    if len(set(question_ids)) != len(question_ids):
        raise ValidationError("a question appears more than once in the order")
    live = {row.id for row in await session.execute(_live_questions([session_type_id]))}
    given = set(question_ids)
    if given != live:
        problems = []
        if live - given:
            problems.append(f"{len(live - given)} question(s) of this form are missing")
        if given - live:
            problems.append(f"{len(given - live)} id(s) are not questions on this form")
        raise ValidationError("; ".join(problems))
    for position, question_id in enumerate(question_ids):
        await session.execute(
            update(SessionTypeQuestion)
            .where(
                SessionTypeQuestion.id == question_id,
                SessionTypeQuestion.session_type_id == session_type_id,
            )
            .values(display_order=position)
        )
    return True


async def _wording(
    session: AsyncSession, answers: list[dict[str, Any]]
) -> tuple[dict[str, str], dict[str, str]]:
    """The current text of every question and chosen option these answers name,
    keyed by the id's string form so a payload's ids match either way."""
    question_ids = [answer["question_id"] for answer in answers]
    option_ids = [oid for answer in answers for oid in (answer.get("option_ids") or [])]
    questions = await session.execute(
        select(SessionTypeQuestion.id, SessionTypeQuestion.question_text).where(
            SessionTypeQuestion.id.in_(question_ids)
        )
    )
    options: dict[str, str] = {}
    if option_ids:
        chosen = await session.execute(
            select(SessionTypeQuestionOption.id, SessionTypeQuestionOption.option_text).where(
                SessionTypeQuestionOption.id.in_(option_ids)
            )
        )
        options = {str(oid): text for oid, text in chosen}
    return {str(qid): text for qid, text in questions}, options


async def record_answers(
    session: AsyncSession,
    *,
    session_id: UUID,
    mentee_id: UUID,
    answers: list[dict[str, Any]],
) -> None:
    """Store a booking's answers: one submission, one row per answer or option.

    **Called only with answers already checked** by `answer_problems` against
    this offering's form, so every question and option id here is one it asks.
    Nothing is written for no answers — an empty form is not a submission. A
    multiple-choice answer is one row per chosen option (#207); a file answer
    carries the `file_storage_key` its upload was linked under. Does not commit:
    the booking's transaction owns it, so a session and its answers land
    together or not at all.

    **Each row keeps the wording it answered** (#350): the question's text, and
    a choice row's option text, as they read now, so a later rewording never
    puts an old answer under new words.
    """
    if not answers:
        return
    question_text, option_text = await _wording(session, answers)
    submission_id = (
        await session.execute(
            insert(IntakeSubmission)
            .values(
                session_id=session_id,
                mentee_id=mentee_id,
                status=IntakeStatus.SUBMITTED,
                submitted_at=func.now(),
            )
            .returning(IntakeSubmission.id)
        )
    ).scalar_one()
    rows: list[dict[str, Any]] = []
    for answer in answers:
        base = {
            "submission_id": submission_id,
            "question_id": answer["question_id"],
            "question_text": question_text[str(answer["question_id"])],
        }
        if answer.get("file_storage_key") is not None:
            rows.append({**base, "file_storage_key": answer["file_storage_key"]})
        elif answer.get("option_ids") is not None:
            rows += [
                {
                    **base,
                    "selected_option_id": option_id,
                    "option_text": option_text[str(option_id)],
                }
                for option_id in answer["option_ids"]
            ]
        else:
            rows.append({**base, "answer_text": answer["text"]})
    await session.execute(insert(IntakeAnswer), rows)
