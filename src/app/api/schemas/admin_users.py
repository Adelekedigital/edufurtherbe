"""A user an admin may pick to preview: the picker's row (decision 310).

One shape for every place the admin names a previewable user: the search's rows,
and the preview session's ``user``, which is to reuse it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, Field

from app.api.schemas.common import StoredEmail
from app.domain.enums import PrimaryRole

__all__ = ["PAGE_SIZE", "AdminUserRead"]

#: Rows a page of the search holds (the frontend contract).
PAGE_SIZE = 20


class AdminUserRead(BaseModel):
    """Who the user is, as the picker shows them. Either name may be null: a
    migrated user may have none, and the picker then shows the email."""

    user_id: UUID
    email: StoredEmail
    first_name: str | None
    last_name: str | None
    role: PrimaryRole = Field(
        description="`users.primary_role`, the same value `GET /me` returns as `primary_role`."
    )

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Self:
        return cls(
            user_id=row["user_id"],
            email=row["email"],
            first_name=row["first_name"],
            last_name=row["last_name"],
            role=row["role"],
        )
