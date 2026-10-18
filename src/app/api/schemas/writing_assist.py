"""`POST /me/writing-assist` over the wire (decision 267)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import WritingAssistField, WritingAssistMode
from app.domain.profile_text import ABOUT_ME_MAX_LENGTH


class WritingAssistWrite(BaseModel):
    """What to write, and for an improve, the text to start from."""

    model_config = ConfigDict(extra="forbid")

    field: WritingAssistField = Field(
        description=(
            "`mentee_bio` for a mentee's bio, `mentor_about` for a mentor's. Both "
            "are the profile's `about_me`; they are counted apart for the daily limit."
        )
    )
    mode: WritingAssistMode = Field(
        description="`draft` writes a bio from your saved profile; `improve` rewrites `text`."
    )
    #: Bounded only loosely here; the rule (required for an improve, at most
    #: `ABOUT_ME_MAX_LENGTH` characters, refused for a draft) is the route's,
    #: so its `422` points at `/text` with a message a person can read.
    text: str | None = Field(
        default=None,
        max_length=10 * ABOUT_ME_MAX_LENGTH,
        description=(
            f"The text to improve, at most {ABOUT_ME_MAX_LENGTH} characters. "
            "Required for `improve`; not sent for `draft`."
        ),
    )


class WritingAssistRead(BaseModel):
    """A suggestion. **Nothing is saved**: the client shows it, and the person
    saves it through `PATCH /users/{id}/profile` if they keep it."""

    text: str = Field(
        description=f"The suggested bio, plain text, at most {ABOUT_ME_MAX_LENGTH} characters."
    )
    prompt_version: str = Field(
        description=(
            "Which prompt wrote it, e.g. `bio_draft-v1`. Send it back as "
            "`about_me_prompt_version` with `about_me_drafted: true` when the "
            "person saves this text."
        ),
        examples=["bio_draft-v1"],
    )
