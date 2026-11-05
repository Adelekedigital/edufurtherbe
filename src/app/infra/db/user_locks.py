"""The per-user row lock every credit-level writer serialises on.

**One lock, one copy** (non-negotiable #8). The unlock (decision 295) and the
level grants (decision 307) each read facts another writer may be changing — a
qualification, a settlement, the 1st's grant — so they all queue on the same
thing: the users' rows, locked in id order.
"""

from __future__ import annotations

from collections.abc import Collection
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.db.models.user import User

__all__ = ["lock_users"]


async def lock_users(session: AsyncSession, user_ids: Collection[UUID]) -> None:
    """Lock these users' rows until the transaction ends, in id order so two
    writers locking overlapping sets cannot deadlock on each other.

    **`FOR NO KEY UPDATE`, not `FOR UPDATE`**: it serialises these writers with
    each other (and with account deletion's `FOR UPDATE`) but not with the
    `FOR KEY SHARE` every foreign-key insert takes on a user, so a credit lot or
    a booking for the same user neither waits on it nor closes a deadlock cycle.
    Re-locking a row this transaction already holds is a no-op.
    """
    if not user_ids:
        return
    await session.execute(
        select(User.id)
        .where(User.id.in_(list(user_ids)))
        .order_by(User.id)
        .with_for_update(key_share=True)
    )
