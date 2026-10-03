"""The rows behind a session's intake answers, and how they fold into answers.

**One definition for two readers.** ``GET /sessions/{id}/answers`` lists every
answer, and every ``SessionRead`` carries a preview of the same list: how many
answers there are and the first one. Both read *this* statement and *this*
fold, so the preview cannot count a different set, order it differently, or
word a question differently from the list it previews.

**Scoping is the caller's.** This module carries no reader predicate of its
own: ``answer_rows`` takes the filters, and each caller passes the one it
answers for. The answers endpoint passes the party-or-admin predicate; the
session reads pass ids they have already scoped to the viewer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping, Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import QuestionType
from app.infra.db.models.intake import (
    IntakeAnswer,
    IntakeFile,
    IntakeSubmission,
    SessionTypeQuestion,
    SessionTypeQuestionOption,
)
from app.infra.db.models.sessions import Session

__all__ = ["answer_previews", "answer_rows", "fold_answers", "preview_text"]


def answer_rows(*filters: Any) -> Select[Any]:
    """One row per answer and chosen option, in form order, narrowed by ``filters``.

    The form's order is the question's ``display_order`` and, inside a choice,
    the option's ``sort_order`` — not the order the mentee picked them in. Ties
    break on the ids, so the order is stable.
    """
    return (
        select(
            IntakeSubmission.session_id,
            SessionTypeQuestion.id.label("question_id"),
            SessionTypeQuestion.question_text,
            SessionTypeQuestion.deleted_at.is_not(None).label("retired"),
            IntakeAnswer.answer_text,
            SessionTypeQuestionOption.id.label("option_id"),
            SessionTypeQuestionOption.option_text,
            IntakeFile.id.label("file_id"),
            IntakeFile.filename,
            IntakeFile.content_type,
            IntakeFile.size_bytes,
            IntakeFile.deleted_at.is_(None).label("file_available"),
        )
        .select_from(IntakeAnswer)
        .join(IntakeSubmission, IntakeSubmission.id == IntakeAnswer.submission_id)
        .join(Session, Session.id == IntakeSubmission.session_id)
        .join(SessionTypeQuestion, SessionTypeQuestion.id == IntakeAnswer.question_id)
        .outerjoin(
            SessionTypeQuestionOption,
            SessionTypeQuestionOption.id == IntakeAnswer.selected_option_id,
        )
        .outerjoin(IntakeFile, IntakeFile.storage_key == IntakeAnswer.file_storage_key)
        .where(*filters)
        .order_by(
            SessionTypeQuestion.display_order,
            SessionTypeQuestion.id,
            SessionTypeQuestionOption.sort_order,
            SessionTypeQuestionOption.id,
        )
    )


def fold_answers(rows: Iterable[RowMapping]) -> dict[UUID, list[dict[str, Any]]]:
    """Each session's answers, one entry per answered question, in row order."""
    sessions: dict[UUID, dict[UUID, dict[str, Any]]] = {}
    for row in rows:
        entry = sessions.setdefault(row["session_id"], {}).setdefault(
            row["question_id"],
            {
                "question_id": row["question_id"],
                "question_text": row["question_text"],
                "retired": row["retired"],
                "text": None,
                "options": [],
                "file": None,
            },
        )
        # **The type is the answer's form, not the question's type now.** A
        # mentor may switch a question between free text and file after it was
        # answered, and the current type would then mislabel the answer given.
        # The CHECK guarantees each row carries exactly one form.
        if row["answer_text"] is not None:
            entry["question_type"] = QuestionType.FREE_TEXT
            entry["text"] = row["answer_text"]
        if row["option_id"] is not None:
            entry["question_type"] = QuestionType.MULTI_CHOICE
            entry["options"].append({"id": row["option_id"], "text": row["option_text"]})
        if row["file_id"] is not None:
            entry["question_type"] = QuestionType.FILE_UPLOAD
            entry["file"] = {
                "id": row["file_id"],
                "filename": row["filename"],
                "content_type": row["content_type"],
                "size_bytes": row["size_bytes"],
                "available": row["file_available"],
            }
    return {session_id: list(entries.values()) for session_id, entries in sessions.items()}


def preview_text(answer: Mapping[str, Any]) -> str:
    """One answer as a line of plain text: the text, the choices, or the file's name."""
    if answer["text"] is not None:
        return str(answer["text"])
    if answer["options"]:
        return ", ".join(str(option["text"]) for option in answer["options"])
    if answer["file"] is not None:
        return str(answer["file"]["filename"])
    return ""


async def answer_previews(
    session: AsyncSession, session_ids: Sequence[UUID]
) -> dict[UUID, dict[str, Any]]:
    """How many answers each session has, and its first, in one query for a page.

    ``session_ids`` must already be scoped to the viewer: this adds no reader
    check. A session with no answers is absent from the result.
    """
    if not session_ids:
        return {}
    rows = await session.execute(answer_rows(IntakeSubmission.session_id.in_(list(session_ids))))
    return {
        session_id: {
            "count": len(answers),
            "first": {
                "question_text": answers[0]["question_text"],
                "text": preview_text(answers[0]),
            },
        }
        for session_id, answers in fold_answers(rows.mappings()).items()
    }
