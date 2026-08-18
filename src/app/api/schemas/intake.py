"""The intake form, as the mentor who owns it sees it.

No mentee-facing model yet: answering is the next surface. What is here is the
*definition* — what an offering asks, in what order, and whether an answer is
required.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, Field, model_validator

from app.api.schemas.common import Normalised
from app.domain.enums import QuestionType

#: **Narrower than the column's vocabulary, deliberately.**
#:
#: `question_type` accepts `multi_choice` in the database, because the canonical
#: package declares it and `session_type_question_options` exists to hold its
#: choices. Nothing can *create* an option yet — that surface follows its
#: consumer under #21 — so a `multi_choice` question created here would be a
#: question no mentee could answer.
#:
#: The same shape as `ConferencingProvider` against `MeetingProvider`: what may
#: be chosen now is a subset of what the column can hold, and the subset widens
#: when the missing surface arrives.
SELECTABLE_TYPES = frozenset({QuestionType.FREE_TEXT, QuestionType.FILE_UPLOAD})


def _refuse_unbuildable_type(question_type: QuestionType | None) -> None:
    if question_type is not None and question_type not in SELECTABLE_TYPES:
        raise ValueError(
            f"{question_type.value!r} questions need answer options, and there is no way "
            "to add them yet — use free_text or file_upload"
        )


class QuestionRead(BaseModel):
    """One question on your form."""

    id: str
    question_text: str
    question_type: QuestionType = Field(
        description=(
            "`free_text` for prose, `file_upload` for a document. `multi_choice` "
            "exists in the schema and is **not selectable**: its answer options "
            "have no surface to create them yet."
        )
    )
    is_required: bool = Field(
        description="Whether a mentee must answer before the form can be submitted."
    )
    display_order: int = Field(
        description=(
            "Ascending. Ties break on creation order, so a form where every "
            "question left this at `0` still has a stable order rather than one "
            "that shifts between requests."
        )
    )

    @classmethod
    def from_row(cls, row: dict[str, object]) -> QuestionRead:
        return cls(
            id=str(row["id"]),
            question_text=str(row["question_text"]),
            question_type=QuestionType(str(row["question_type"])),
            is_required=bool(row["is_required"]),
            display_order=int(str(row["display_order"])),
        )


class QuestionWrite(Normalised):
    """A new question."""

    question_text: str = Field(max_length=500)
    question_type: QuestionType = QuestionType.FREE_TEXT
    is_required: bool = False
    display_order: int = Field(default=0, ge=0, le=100)

    @model_validator(mode="after")
    def _type_is_buildable(self) -> Self:
        _refuse_unbuildable_type(self.question_type)
        return self


class QuestionPatch(Normalised):
    """A change to one question. Absent is not null."""

    question_text: str | None = Field(default=None, max_length=500)
    question_type: QuestionType | None = None
    is_required: bool | None = None
    display_order: int | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def _type_is_buildable(self) -> Self:
        _refuse_unbuildable_type(self.question_type)
        return self
