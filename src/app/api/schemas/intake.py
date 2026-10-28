"""The intake form, as the mentor who owns it sees it.

Plus `IntakeFileRead`, the one mentee-facing model: a file uploaded to answer a
`file_upload` question with. The rest is the *definition* — what an offering
asks, in what order, whether an answer is required, and for a choice question
its options (#200).
"""

from __future__ import annotations

from typing import Any, Self, cast
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from app.api.schemas.common import Normalised, refuse_repeats
from app.domain.enums import IntakeFileType, QuestionOptionsSource, QuestionType
from app.domain.intake import MAX_OPTION_LENGTH, MAX_OPTIONS, MIN_OPTIONS


class OptionRead(BaseModel):
    """One option of a choice question, in the order the mentor set, or one of
    the mentor's help areas on a help-area question."""

    id: str = Field(
        description=(
            "The option's id; on a help-area question, the service offering's id "
            "(`/api/v1/catalog/service-offerings`). Send it in `answers[].option_ids`."
        )
    )
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
    refuse_repeats([option.text.casefold() for option in options], "two options have the same text")


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
        description=(
            "Whether a mentee must answer before the form can be submitted. On the "
            "public form (`SessionTypeRead.questions`), `false` for a choice question "
            "with no options, which booking never requires; your own list keeps "
            "your setting."
        )
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
        description=(
            "A `multi_choice` question's options, in order. Empty for other types. On "
            "a help-area question (`options_source: help_areas`), the mentor's current "
            "help areas in the platform's order, read fresh each time, so they follow "
            "the profile; empty if the mentor has none (see `is_required`)."
        )
    )
    # Always sent; the default only keeps it out of `required` for the frontend's CI.
    options_source: QuestionOptionsSource | None = Field(
        default=None,
        description=(
            "`help_areas`: the options are the mentor's help areas, not a list they "
            "wrote. `null`: the mentor wrote `options` (or the question is not a choice)."
        ),
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
            options_source=(
                QuestionOptionsSource(str(row["options_source"]))
                if row.get("options_source")
                else None
            ),
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
            f"For `multi_choice` only: {MIN_OPTIONS} to {MAX_OPTIONS} options, unique "
            "ignoring case, in the order to show them. Required by `multi_choice` "
            "unless `options_source` is set, when sending any is a `422`."
        ),
    )
    options_source: QuestionOptionsSource | None = Field(
        default=None,
        description=(
            "`help_areas` makes the options your help areas, kept in step with your "
            "profile: `multi_choice` only (either `allows_multiple`), and sent "
            "**without** `options` (a `422` otherwise). Set when the question is "
            "created and never changed: delete the question and add another."
        ),
    )

    @model_validator(mode="after")
    def _options_match_the_type(self) -> Self:
        if self.options_source is not None:
            if self.question_type is not QuestionType.MULTI_CHOICE:
                raise ValueError("options_source is only for multi_choice questions")
            if self.options:
                raise ValueError("a help-area question takes no options: they are your help areas")
        elif self.question_type is QuestionType.MULTI_CHOICE:
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
            "out is removed — a `409` if a booking answer chose it. A `422` on a "
            "help-area question, whose options are your help areas."
        ),
    )
    #: **Refused, not ignored**: a question never crosses between written and
    #: help-area options, so an answer never changes meaning (decision 200's rule).
    #: Hidden so the `422` points at it; never dumped, never published.
    options_source: SkipJsonSchema[object] = Field(default=None, exclude=True)

    @field_validator("options_source", mode="before")
    @classmethod
    def _fixed_source(cls, _value: object) -> object:
        raise ValueError(
            "options_source is set when a question is created; delete it and add another"
        )

    @model_validator(mode="after")
    def _options_are_well_formed(self) -> Self:
        if self.options is not None:
            _check_options(self.options)
            refuse_repeats(
                [o.id for o in self.options if o.id is not None],
                "an option id appears more than once",
            )
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


class IntakeFileRead(BaseModel):
    """A file you uploaded: to answer a `file_upload` question with
    (`POST /me/intake-files`), or to send in a message (`POST /me/message-files`)."""

    file_id: UUID = Field(
        description=(
            "Send as `answers[].file_id` when booking (an intake upload), or as "
            "`file_id` on a message (a message file)."
        )
    )
    filename: str = Field(
        description=(
            "The name it will download as: your file's name without any path, "
            "ending in the extension of what the file actually is."
        )
    )
    size: int = Field(description="Bytes.")
    content_type: IntakeFileType = Field(description="Decided from the file's bytes.")


class AnsweredOptionRead(BaseModel):
    """A choice the mentee picked."""

    id: UUID = Field(
        description="The option's id; on a help-area question, the service offering's id."
    )
    text: str = Field(
        description="The option's wording when it was chosen, or its current wording for "
        "answers given before that was kept."
    )


class AnsweredFileRead(BaseModel):
    """The file that answers a `file_upload` question."""

    id: UUID = Field(description="Download with `GET /api/v1/intake-files/{id}`.")
    filename: str
    content_type: IntakeFileType
    size: int = Field(description="Bytes.")
    available: bool = Field(
        description=(
            "`false` once retention has removed the file: the answer stays, the "
            "download is a `404`. Show the name without a link."
        )
    )


class SessionAnswerRead(BaseModel):
    """One question of the booking's intake form and the mentee's answer to it.

    Exactly one of `text`, `options` (non-empty) or `file` carries the answer,
    by `question_type`.
    """

    question_id: UUID
    question_text: str = Field(
        description=(
            "The question's wording **as the mentee saw it**, kept at booking: a "
            "mentor who rewords a question afterwards still sees the words that "
            "were answered. Answers given before that copy was kept show the "
            "current wording."
        )
    )
    question_type: QuestionType = Field(
        description=(
            "The form the **answer** was given in. A mentor may switch a question "
            "between free text and file after it was answered; this follows the "
            "answer, so it always says which of `text`, `options` or `file` to read."
        )
    )
    retired: bool = Field(
        description="The question has since been removed from the form. Its answer is still shown."
    )
    text: str | None = Field(default=None, description="For `free_text`.")
    options: list[AnsweredOptionRead] = Field(
        default_factory=list,
        description="For `multi_choice`, in the order the form lists them.",
    )
    file: AnsweredFileRead | None = Field(default=None, description="For `file_upload`.")
    answered: bool = Field(
        default=True,
        description=(
            "`false` for a question on the form that the mentee left blank: show "
            "it as *No answer*. A booking's form is kept as it stood when it was "
            "made, so every question asked is listed, in that order. Bookings made "
            "before the form was kept list only their answers, all `true`."
        ),
    )
    required: bool | None = Field(
        default=None,
        description=(
            "Whether the question was required when the booking was made; `null` "
            "for a booking made before the form was kept."
        ),
    )

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> SessionAnswerRead:
        file = row["file"]
        return cls(
            question_id=row["question_id"],
            question_text=row["question_text"],
            question_type=QuestionType(str(row["question_type"])),
            retired=row["retired"],
            answered=row.get("answered", True),
            required=row.get("required"),
            text=row["text"],
            options=[AnsweredOptionRead(**option) for option in row["options"]],
            file=(
                AnsweredFileRead(
                    id=file["id"],
                    filename=file["filename"],
                    content_type=IntakeFileType(str(file["content_type"])),
                    size=file["size_bytes"],
                    available=file["available"],
                )
                if file is not None
                else None
            ),
        )
