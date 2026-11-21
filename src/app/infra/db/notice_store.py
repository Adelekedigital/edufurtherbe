"""The notices a user dismissed: record one, and list them (decision 356).

**Every statement names the caller**: there is no way to read or write another
user's notices, because nothing here takes a user but the one it is given.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Result, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.seen_items import MAX_SEEN_ITEMS
from app.infra.db.models.notices import UserDismissedNotice
from app.infra.db.per_user_cap import keep_newest
from app.infra.db.user_locks import lock_users

__all__ = ["dismiss_notice", "dismissed_notices"]


async def dismiss_notice(session: AsyncSession, user_id: UUID, notice_key: str) -> None:
    """Record that the user dismissed ``notice_key``. A repeat keeps the first
    time and changes nothing. A new key past `MAX_SEEN_ITEMS` drops the
    user's oldest. **The user is locked first**, so two new keys at once cannot
    both keep one too many. Does not commit."""
    await lock_users(session, {user_id})
    result: Result[tuple[UUID]] = await session.execute(
        insert(UserDismissedNotice)
        # The wall clock after the lock, not `now()`: `now()` is when the
        # transaction began, so a request that waited for the lock would store
        # an earlier time than a row committed before it and the cap could drop
        # the key just written.
        .values(user_id=user_id, notice_key=notice_key, created_at=func.clock_timestamp())
        .on_conflict_do_nothing(constraint="uq_user_dismissed_notices_user_id_notice_key")
        .returning(UserDismissedNotice.id)
    )
    if result.first() is not None:
        await keep_newest(
            session,
            UserDismissedNotice,
            UserDismissedNotice.user_id,
            user_id,
            newest=UserDismissedNotice.created_at,
            keep=MAX_SEEN_ITEMS,
        )


async def dismissed_notices(session: AsyncSession, user_id: UUID) -> list[str]:
    """The user's dismissed notice keys, the first dismissed first."""
    rows = await session.scalars(
        select(UserDismissedNotice.notice_key)
        .where(UserDismissedNotice.user_id == user_id)
        .order_by(UserDismissedNotice.created_at, UserDismissedNotice.id)
    )
    return list(rows)
