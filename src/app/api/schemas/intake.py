"""The intake form, as the mentor who owns it sees it.

No mentee-facing model yet: answering is the next surface. What is here is the
*definition* — what an offering asks, in what order, whether an answer is
required, and for a choice question its options (#200).
"""

from __future__ import annotations

from typing import Self, cast
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.api.schemas.common import Normalised
from app.domain.enums import QuestionType
from app.domain.intake import MAX_OPTION_LENGTH, MAX_OPTIONS, MIN_OPTIONS


class OptionRead(BaseModel):
    """One option of a choice question, in the order the mentor set."""

    id: str
    text: str


class OptionWrite(Normalised):
    """One option of a new choice question."""

    text: str = Field(min_length=1, max_length=MAX_OPTION_LENGTH)


class OptionPatch(Normalised):
    """One option in a replacement list: an `id` keeps that option, none adds one."""

    id: UUID | None = Field(
        default=None,
        description="An existing option of this question, kept (its text may change). "
        "Omit to add a new option.",
    )
    text: str = Field(min_length=1, max_length=MAX_OPTION_LENGTH)


def _check_options(options: list[OptionWrite] | list[OptionPatch]) -> None:
    """Between MIN_OPTIONS and MAX_OPTIONS, and no two the same (ignoring case)."""
    if not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
        raise ValueError(
            f"a choice question needs {MIN_OPTIONS} to {MAX_OPTIONS} options; {len(options)} given"
        )
    texts = [option.text.casefold() for option in options]
    if len(set(texts)) != len(texts):
        raise ValueError("two options have the same text")


class QuestionRead(BaseModel):
    """One question on your form."""

    id: str
    question_text: str
    question_type: QuestionType = Field(
        description=(
            "`free_text` for prose, `file_upload` for a document, `multi_choice` "
            "to choose from `options` — one, or several when `allows_multiple`."
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
    allows_multiple: bool = Field(
        description="For `multi_choice`: several options may be picked. False is single choice."
    )
    options: list[OptionRead] = Field(
        description="A `multi_choice` question's options, in order. Empty for other types."
    )

    @classmethod
    def from_row(cls, row: dict[str, object]) -> QuestionRead:
        options = cast("list[dict[str, object]]", row.get("options") or [])
        return cls(
            id=str(row["id"]),
            question_text=str(row["question_text"]),
            question_type=QuestionType(str(row["question_type"])),
            is_required=bool(row["is_required"]),
            display_order=int(str(row["display_order"])),
            allows_multiple=bool(row.get("allows_multiple", False)),
            options=[OptionRead(id=str(o["id"]), text=str(o["text"])) for o in options],
        )


class QuestionWrite(Normalised):
    """A new question."""

    question_text: str = Field(max_length=500)
    question_type: QuestionType = QuestionType.FREE_TEXT
    is_required: bool = False
    display_order: int = Field(default=0, ge=0, le=100)
    allows_multiple: bool = Field(
        default=False,
        description="For `multi_choice` only: several options may be picked.",
    )
    options: list[OptionWrite] = Field(
        default_factory=list,
        description=(
            f"For `multi_choice` only, and required by it: {MIN_OPTIONS} to "
            f"{MAX_OPTIONS} options, unique ignoring case, in the order to show them."
        ),
    )

    @model_validator(mode="after")
    def _options_match_the_type(self) -> Self:
        if self.question_type is QuestionType.MULTI_CHOICE:
            _check_options(self.options)
        elif self.options or self.allows_multiple:
            raise ValueError("options and allows_multiple are only for multi_choice questions")
        return self


class QuestionPatch(Normalised):
    """A change to one question. Absent is not null."""

    question_text: str | None = Field(default=None, max_length=500)
    question_type: QuestionType | None = None
    is_required: bool | None = None
    display_order: int | None = Field(default=None, ge=0, le=100)
    allows_multiple: bool | None = None
    options: list[OptionPatch] | None = Field(
        default=None,
        description=(
            "Replaces the options of a `multi_choice` question: each with an `id` is "
            "kept (text and position may change), each without is added, and one left "
            "out is removed — a `409` if a booking answer chose it."
        ),
    )

    @model_validator(mode="after")
    def _options_are_well_formed(self) -> Self:
        if self.options is not None:
            _check_options(self.options)
            ids = [o.id for o in self.options if o.id is not None]
            if len(set(ids)) != len(ids):
                raise ValueError("an option id appears more than once")
        return self


class QuestionOrderWrite(BaseModel):
    """The form's questions in their new order: every live question, once (#197)."""

    question_ids: list[UUID] = Field(
        max_length=50,
        description=(
            "Every live question on the form, each once, in the order to show them. "
            "Renumbered from zero. Missing, extra or repeated ids are a `422`."
        ),
    )
