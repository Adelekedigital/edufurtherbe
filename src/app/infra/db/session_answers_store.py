"""A booked session's intake answers, for the three people who may read them.

**Three readers, and the same three as the file behind a file answer**
(decision 210): the session's mentee, its mentor, and any live admin. The
check is in the query — the session's party predicate, `is_a_party`, the one
`GET /sessions/{id}` uses — so a caller outside the three reads exactly what a
caller holding a made-up id reads: nothing, which the route answers as 404.

**The question reads as it stands now, not as it stood at booking.** Nothing
keeps a copy of the wording when a mentee answers, so a mentor who rewords a
question after a booking sees the new wording beside the old answer. A *retired*
question is still shown, because retiring is a soft delete that keeps the row
for exactly this read, and it is flagged `retired`. The same holds for an
option's text. Whether to keep a copy at booking is #350's question.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.db.models.intake import (
    IntakeAnswer,
    IntakeFile,
    IntakeSubmission,
    SessionTypeQuestion,
    SessionTypeQuestionOption,
)
from app.infra.db.models.sessions import Session
from app.infra.db.session_store import is_a_party

__all__ = ["session_answers"]


def _readable_by(caller_id: UUID, caller_is_admin: bool) -> Any:
    """The session's two parties, or an admin. One predicate for both reads."""
    return or_(is_a_party(caller_id), literal(caller_is_admin))


async def session_answers(
    session: AsyncSession, session_id: UUID, *, caller_id: UUID, caller_is_admin: bool
) -> list[dict[str, Any]] | None:
    """The answers, one entry per question in form order; ``None`` if unreadable.

    ``None`` is "no such session **or** not yours", which the route turns into
    one 404. A readable session with no form, or a migrated one, is an empty
    list: it exists and it was asked nothing.
    """
    readable = await session.scalar(
        select(Session.id).where(Session.id == session_id, _readable_by(caller_id, caller_is_admin))
    )
    if readable is None:
        return None

    # Scoped again here rather than trusting the check above: the rows are
    # personal data, and the predicate costs one join.
    rows = await session.execute(
        select(
            SessionTypeQuestion.id.label("question_id"),
            SessionTypeQuestion.question_text,
            SessionTypeQuestion.question_type,
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
        .where(Session.id == session_id, _readable_by(caller_id, caller_is_admin))
        .order_by(
            SessionTypeQuestion.display_order,
            SessionTypeQuestion.id,
            SessionTypeQuestionOption.sort_order,
            SessionTypeQuestionOption.id,
        )
    )

    answers: dict[UUID, dict[str, Any]] = {}
    for row in rows.mappings():
        entry = answers.setdefault(
            row["question_id"],
            {
                "question_id": row["question_id"],
                "question_text": row["question_text"],
                "question_type": row["question_type"],
                "retired": row["retired"],
                "text": None,
                "options": [],
                "file": None,
            },
        )
        if row["answer_text"] is not None:
            entry["text"] = row["answer_text"]
        if row["option_id"] is not None:
            entry["options"].append({"id": row["option_id"], "text": row["option_text"]})
        if row["file_id"] is not None:
            entry["file"] = {
                "id": row["file_id"],
                "filename": row["filename"],
                "content_type": row["content_type"],
                "size_bytes": row["size_bytes"],
                "available": row["file_available"],
            }
    return list(answers.values())
