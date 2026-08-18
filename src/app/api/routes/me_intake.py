"""A mentor's intake form — what their offering asks before a session runs.

**Its own module rather than more of `me_session_types.py`.** That file holds the
four operations on an offering; these are four on a *sub-resource* of one, and
keeping them apart is what stops a reader believing there is one surface with
eight endpoints. The same reasoning that split `me_session_types.py` out of
`session_types.py` in the first place.

**Nested under the offering, because the offering is what carries ownership.**
`/me/session-types/{id}/questions` reaches a question through the row that says
whose it is; a flat `/me/questions/{id}` would have to look the question up and
then check, which is the shape non-negotiable #5 refuses.

There is no mentee-facing surface here yet. Answering is the next release, and
`intake_submissions` and `intake_answers` are waiting for it.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.api.deps import (
    CreatedOwnQuestionDep,
    DeletedOwnQuestionDep,
    OwnQuestionsDep,
    UpdatedOwnQuestionDep,
)
from app.api.schemas.common import Page
from app.api.schemas.intake import QuestionRead
from app.core.errors import NotFoundError
from app.domain.intake import MAX_QUESTIONS

router = APIRouter(prefix="/api/v1/me", tags=["users"])

QUESTION_RESPONSES: dict[int | str, dict[str, str]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "The bearer token is absent, malformed, expired or wrongly signed."
    },
    status.HTTP_404_NOT_FOUND: {
        "description": (
            "No such session type, **or** not yours, **or** deleted. "
            "Indistinguishable on purpose: telling them apart says which ids exist."
        )
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "The body failed validation."},
}

FULL_FORM: dict[int | str, dict[str, str]] = {
    status.HTTP_409_CONFLICT: {
        "description": (
            f"This offering already asks {MAX_QUESTIONS} questions. Delete one "
            "before adding another — deleting frees a slot, because the limit "
            "counts live questions."
        )
    }
}


@router.get(
    "/session-types/{session_type_id}/questions",
    response_model=Page[QuestionRead],
    summary="The questions one of your offerings asks",
    description=(
        "Your intake form for this offering, in the order a mentee sees it — "
        "ascending `display_order`, ties broken by creation order so a form "
        "where nothing set it is still stably ordered.\n\n"
        "Deleted questions are absent. Their answers are not deleted: a question "
        "somebody answered is retired rather than removed, so the record of what "
        "was asked survives.\n\n"
        "**A switched-off offering still has a form**, and you can still edit it "
        "— preparing a paused offering is exactly when you would.\n\n"
        "`next_cursor` is always `null`. A form holds at most "
        f"{MAX_QUESTIONS} questions, so it is returned whole; the envelope is "
        "here because ADR 0016 puts it on every list."
    ),
    responses=QUESTION_RESPONSES,
)
async def read_own_questions(questions: OwnQuestionsDep) -> Page[QuestionRead]:
    return Page(data=[QuestionRead.from_row(row) for row in questions], next_cursor=None)


@router.post(
    "/session-types/{session_type_id}/questions",
    status_code=status.HTTP_201_CREATED,
    summary="Add a question to one of your offerings",
    description=(
        f"**At most {MAX_QUESTIONS} questions per offering.** An intake form a "
        "mentee abandons is worse than no form, and the limit is the product's "
        "answer to that. The count is of *live* questions, so deleting one frees "
        "a slot.\n\n"
        "**`multi_choice` is not selectable.** The schema holds it and "
        "`session_type_question_options` exists for its choices, but nothing can "
        "create an option yet — so a `multi_choice` question would be one no "
        "mentee could answer. `free_text` and `file_upload` are the two that "
        "work.\n\n"
        "`display_order` is yours to set and need not be contiguous; leaving it "
        "at `0` puts the question at the end of the questions that also left it "
        "there, in creation order."
    ),
    responses=QUESTION_RESPONSES | FULL_FORM,
)
async def create_own_question(
    question_id: CreatedOwnQuestionDep, session_type_id: str, response: Response
) -> dict[str, str]:
    response.headers["Location"] = f"/api/v1/me/session-types/{session_type_id}/questions"
    return {"id": str(question_id)}


@router.patch(
    "/session-types/{session_type_id}/questions/{question_id}",
    summary="Change one question",
    description=(
        "Every field optional and **absent is not null** — a field you do not "
        "send is left alone.\n\n"
        "Editing a question does not touch answers already given to it. A mentee "
        "who answered the old wording answered the old wording; the record does "
        "not change retroactively, and reworded questions are why a form's "
        "history is worth keeping."
    ),
    responses=QUESTION_RESPONSES,
)
async def edit_own_question(changed: UpdatedOwnQuestionDep) -> dict[str, bool]:
    if not changed:
        raise NotFoundError("no such question")
    return {"updated": True}


@router.delete(
    "/session-types/{session_type_id}/questions/{question_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a question from your form",
    description=(
        "The question leaves your form and stops being asked. **Answers already "
        "given to it survive** — a mentee's submitted form is a record of what "
        "was asked and what they said, and deleting the question out from under "
        "it would leave prose nobody can interpret.\n\n"
        "This frees a slot against the "
        f"{MAX_QUESTIONS}-question limit immediately."
    ),
    responses=QUESTION_RESPONSES,
)
async def remove_own_question(removed: DeletedOwnQuestionDep) -> None:
    if not removed:
        raise NotFoundError("no such question")
