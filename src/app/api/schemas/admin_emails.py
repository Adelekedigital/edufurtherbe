"""The admin Emails pages' shapes (PRD 28, decision 321).

Agreed with the frontend (edufurtherfe-6e, 2026-11-09): lists in the `Page`
envelope, `group` as the key with `group_name` as the words, `recipients` as a
list, and each variant's `label` as the tab's words.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from app.api.schemas.common import StorableText
from app.domain.enums import EmailLine

__all__ = [
    "EmailDetailRead",
    "EmailLastEdited",
    "EmailLineRead",
    "EmailLinesRead",
    "EmailListItem",
    "EmailSaveBody",
    "EmailValueRead",
    "EmailVariantRead",
    "LineSaveBody",
    "SampleRecipient",
    "ValueRead",
]

Recipient = Literal["mentor", "mentee", "admins"]


class EmailLastEdited(BaseModel):
    admin_name: str
    at: dt.datetime


class EmailListItem(BaseModel):
    """One email in the list."""

    key: str = Field(description="The email's key, e.g. `session_booked`.")
    name: str
    trigger: str = Field(description="What sends it, in words.")
    group: str = Field(description="Its shared template's key, or its own key.")
    group_name: str = Field(description="The list card's title.")
    template_name: str = Field(description="The Loops template's name.")
    recipients: list[Recipient]
    status: Literal["live", "not_set_up"] = Field(
        description="`live` once its template is the shared one that shows these lines; "
        "otherwise its lines are read only."
    )
    last_edited: EmailLastEdited | None = Field(
        description="The newest saved change, a reset back to the default included; "
        "null when no line was ever changed."
    )


class ValueRead(BaseModel):
    """A value a line may name as `{name}`."""

    name: str
    meaning: str
    example: str
    fallback: str | None = Field(
        description="What an empty value says in a line at send time; null when the "
        "line cannot go without it."
    )


class EmailValueRead(ValueRead):
    used_in: list[str] = Field(description="The emails that carry it, by name, in list order.")


class EmailLineRead(BaseModel):
    """One line as the editor shows it."""

    default_text: str
    text: str = Field(description="What is sent now: the admin's text, or the default.")
    edited: bool
    version: int = Field(
        description="Send it back with a save. It only rises: 0 for a line never "
        "changed, and a reset line keeps its count."
    )
    default_changed: bool = Field(description="The default moved since this line was edited.")
    new_default: str | None


class EmailLinesRead(BaseModel):
    subject: EmailLineRead
    heading: EmailLineRead
    button: EmailLineRead


class SampleRecipient(BaseModel):
    name: str
    email: str


class EmailVariantRead(BaseModel):
    """One tab: who gets this copy, or which case it is."""

    id: str = Field(description="`default`, `mentor`, `mentee`, `admins` or a case.")
    label: str
    sample: dict[str, str] = Field(
        description="Each value this variant may use, as the preview fills it."
    )
    sample_recipient: SampleRecipient
    lines: EmailLinesRead


class EmailDetailRead(EmailListItem):
    """One email, for the editor."""

    siblings: int = Field(description="How many other emails share its template.")
    values: list[ValueRead]
    variants: list[EmailVariantRead]


#: Generous: the hard max per line is checked by the save, with its own error.
MAX_LINE_INPUT = 500


class LineSaveBody(BaseModel):
    text: Annotated[str, Field(max_length=MAX_LINE_INPUT), StorableText]
    version: int = Field(ge=0, description="The `version` the editor read.")


class EmailSaveLines(BaseModel):
    subject: LineSaveBody
    heading: LineSaveBody
    button: LineSaveBody


class EmailSaveBody(BaseModel):
    """A save of one variant's three lines."""

    lines: EmailSaveLines
    keep_edit: list[EmailLine] = Field(
        default_factory=list,
        description="Lines whose changed default the admin saw and chose to keep.",
    )
