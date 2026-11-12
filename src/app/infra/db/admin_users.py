"""The admin user picker's search: who an admin may preview (decision 310).

Every live user who is not an admin, ordered by what the picker shows: the full
name, or the email when there is none. Keyset on ``(sort key, id)``; the email
is never null, so the key is total, and the id breaks ties. The cursor carries
the id alone and the key is read back from that row, so no name or address
travels in a URL (and from there into an access log).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import case, func, literal, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationError
from app.domain.enums import PrimaryRole
from app.infra.db.models.user import User
from app.infra.db.predicates import LIVE, blank, has_primary_role, is_live_admin, person_like

__all__ = ["find_users", "sort_key"]


def sort_key() -> Any:
    """The name the picker shows, lowercased: first and last joined, or the
    email when that is blank (`blank`). A nameless user sorts among the named by
    their address rather than in a block at either end."""
    name = func.concat_ws(" ", User.first_name, User.last_name)
    return func.lower(case((blank(name), User.email), else_=func.btrim(name)))


async def find_users(
    session: AsyncSession,
    *,
    q: str | None,
    role: PrimaryRole | None,
    after: UUID | None,
    limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    """One page of live non-admin users, and whether another follows.

    ``q`` matches the name or email (`person_like`); ``None`` matches everyone.
    ``role`` filters on `users.primary_role`; ``None`` is every role. ``after``
    is the previous page's last user id; ``limit`` rows a page. An ``after``
    naming no user is a `ValidationError` (a 422), never page one again.
    """
    key = sort_key()
    statement = (
        select(
            User.id.label("user_id"),
            User.email,
            User.first_name,
            User.last_name,
            User.primary_role.label("role"),
        )
        .where(LIVE, ~is_live_admin(User.id))
        .order_by(key, User.id)
        .limit(limit + 1)
    )
    if role is not None:
        statement = statement.where(has_primary_role(role))
    if q:
        statement = statement.where(person_like(User, q))
    if after is not None:
        at = await session.scalar(select(key).where(User.id == after))
        if at is None:
            raise ValidationError("cursor is not a cursor this endpoint issued")
        statement = statement.where(tuple_(key, User.id) > tuple_(literal(at), literal(after)))
    rows = [dict(row) for row in (await session.execute(statement)).mappings()]
    return rows[:limit], len(rows) > limit
