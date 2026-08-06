"""What a user looks like over the wire."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, field_validator

from app.domain.enums import PrimaryRole


class NormalisedEmail(BaseModel):
    """Mixin for any model carrying an email.

    **Normalisation is declarative, not per-handler.** Lowercasing in each route
    is the version that works until somebody adds a route and forgets, and the
    failure is a second account nobody can find. Here it happens before the
    handler is entered, on every model that inherits it.
    """

    email: EmailStr

    @field_validator("email")
    @classmethod
    def normalise(cls, value: str) -> str:
        return value.strip().lower()


class UserProfileRead(BaseModel):
    """The profile fields a user may see about themselves."""

    model_config = ConfigDict(from_attributes=True)

    about_me: str | None = None
    gender: str | None = None
    avatar_url: str | None = None
    banner_url: str | None = None
    social_linkedin: str | None = None
    social_twitter: str | None = None
    social_youtube: str | None = None


class UserRead(NormalisedEmail):
    """A user as returned to themselves.

    ``primary_role`` is included because the client needs it to pick a
    dashboard — which is the *only* thing it is for. It is not an authorization
    claim, and a client treating it as one would be wrong in the same way a
    server would be.

    ``auth_id`` and ``legacy_bubble_id`` are deliberately absent. One is a vendor
    identifier and the other a migration anchor; neither is anybody's business
    outside this service.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    first_name: str | None = None
    last_name: str | None = None
    slug: str | None = None
    primary_role: PrimaryRole
    timezone: str
    email_verified_at: datetime | None = None
    created_at: datetime
    profile: UserProfileRead | None = None
    is_admin: bool = False
