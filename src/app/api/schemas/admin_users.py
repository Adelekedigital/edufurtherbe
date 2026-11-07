"""A user as the admin pages list them: the user search's rows (decision 310)
and the user a preview shows (decision 309). One shape for both."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel

from app.api.schemas.common import StoredEmail
from app.domain.enums import PrimaryRole


class AdminUserRead(BaseModel):
    """One user: who they are and which dashboard is theirs (`users.primary_role`,
    the value `/me` returns)."""

    user_id: UUID
    email: StoredEmail
    first_name: str | None
    last_name: str | None
    role: PrimaryRole
