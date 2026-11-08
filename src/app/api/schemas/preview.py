"""An admin's preview of the app as one user, read only (decision 309)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.api.schemas.admin_users import AdminUserRead
from app.domain.enums import PreviewEndReason
from app.domain.preview import IDLE_WORDS, MAX_LENGTH_WORDS, PAGE_PATH_MAX_LENGTH, expires_at


class PreviewStart(BaseModel):
    """Whom to preview: `user_id` or `email`, exactly one."""

    model_config = ConfigDict(extra="forbid")

    user_id: UUID | None = None
    email: str | None = Field(default=None, min_length=1, max_length=320)

    @model_validator(mode="after")
    def exactly_one(self) -> Self:
        if (self.user_id is None) == (self.email is None):
            raise ValueError("send exactly one of user_id and email")
        return self


class PreviewSwitch(BaseModel):
    """Whom to preview instead, in the same preview."""

    model_config = ConfigDict(extra="forbid")

    user_id: UUID


class PreviewExit(BaseModel):
    """Why the admin's side ended the preview: they left it, or signed out."""

    model_config = ConfigDict(extra="forbid")

    reason: Literal[PreviewEndReason.EXIT, PreviewEndReason.SIGN_OUT]


class PreviewPageView(BaseModel):
    """A page opened in the preview: its path only. A query string or fragment
    sent with it is dropped before anything is checked or stored."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=PAGE_PATH_MAX_LENGTH, pattern=r"^/")

    @field_validator("path", mode="before")
    @classmethod
    def path_only(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.split("#", 1)[0].split("?", 1)[0]
        return value


class PreviewSessionRead(BaseModel):
    """One preview, live or ended."""

    id: UUID
    user: AdminUserRead = Field(description="The user being previewed now.")
    started_at: datetime
    expires_at: datetime = Field(
        description=(f"`started_at` plus {MAX_LENGTH_WORDS}: the preview ends then at the latest.")
    )
    ended_at: datetime | None
    end_reason: PreviewEndReason | None = Field(
        description=(
            "Why it ended: `exit` (the admin left, or started another preview in "
            f"this sign-in), `idle` ({IDLE_WORDS} with no request), `max_length` "
            f"({MAX_LENGTH_WORDS} since it started) or `sign_out`. Null while live."
        )
    )
    revealed: bool = Field(
        description="Whether the admin revealed this user's messages; a switch clears it."
    )
    can_reveal: bool = Field(description="Whether the caller's grant may reveal messages.")

    @classmethod
    def of(cls, row: Any, *, can_reveal: bool) -> PreviewSessionRead:
        return cls(
            id=row.id,
            user=AdminUserRead.from_row(row._mapping),
            started_at=row.started_at,
            expires_at=expires_at(row.started_at),
            ended_at=row.ended_at,
            end_reason=row.end_reason,
            revealed=row.revealed_at is not None,
            can_reveal=can_reveal,
        )
