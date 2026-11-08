"""The admin user picker's search (decision 310). Every live grant may search
(`QueueViewerDep`), as on the Mentors page."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import Depends, Query

from app.api.deps.core import QueueViewerDep, SessionDep
from app.api.schemas.admin_users import PAGE_SIZE, AdminUserRead
from app.api.schemas.common import (
    MAX_QUERY_LENGTH,
    Page,
    StorableText,
    decode_id_cursor,
    encode_id_cursor,
)
from app.domain.enums import PrimaryRole
from app.infra.db.admin_users import find_users


async def admin_users(
    _: QueueViewerDep,
    session: SessionDep,
    q: Annotated[
        str | None,
        Query(
            max_length=MAX_QUERY_LENGTH,
            description="Part of an email, first, last or full name, ignoring case.",
        ),
        StorableText,
    ] = None,
    role: Annotated[
        Literal["all"] | PrimaryRole,
        Query(description="`all`, or one `primary_role`."),
    ] = "all",
    cursor: Annotated[
        str | None, Query(description="`next_cursor` from the previous page.")
    ] = None,
) -> Page[AdminUserRead]:
    rows, has_more = await find_users(
        session,
        q=(q or "").strip() or None,
        role=None if role == "all" else PrimaryRole(role),
        after=decode_id_cursor(cursor),
        limit=PAGE_SIZE,
    )
    next_cursor = encode_id_cursor(rows[-1]["user_id"]) if has_more else None
    return Page(data=[AdminUserRead.from_row(row) for row in rows], next_cursor=next_cursor)


AdminUsersDep = Annotated[Page[AdminUserRead], Depends(admin_users)]
