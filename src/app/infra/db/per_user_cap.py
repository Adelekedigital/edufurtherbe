"""Keeping only a user's newest rows in a table that must not grow without bound.

One statement of it, read by the push subscriptions (`push_store`, ten
browsers) and the dismissed notices (`notice_store`, decision 356): rule 8's
one representation of "keep the newest N of this user's rows".
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["keep_newest"]


async def keep_newest(
    session: AsyncSession, model: Any, owner: Any, user_id: UUID, *, newest: Any, keep: int
) -> None:
    """Delete ``user_id``'s rows of ``model`` beyond the ``keep`` newest by
    ``newest`` (the row id breaks a tie). ``owner`` is the model's user column.

    The caller holds the user's lock (`user_locks.lock_users`) when two writes
    at once could each keep one too many. Does not commit."""
    kept = (
        select(model.id)
        .where(owner == user_id)
        .order_by(newest.desc(), model.id.desc())
        .limit(keep)
    )
    await session.execute(delete(model).where(owner == user_id, model.id.not_in(kept)))
