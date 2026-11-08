"""A user's Web Push subscriptions: subscribe, unsubscribe, and whether (decision 312).

**Every statement names the caller**, except the one that hands a browser to
them: an endpoint is one browser profile, so subscribing an endpoint another
account holds moves it, keys and all, to whoever signed in there last. Removing
and asking are scoped by `user_id`, so knowing somebody's endpoint removes and
reveals nothing of theirs.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import Result, delete, exists, literal_column, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.push import MAX_SUBSCRIPTIONS_PER_USER
from app.infra.clients.web_push import PushOutcome
from app.infra.db.models.push import PushSubscription
from app.infra.db.models.user import User
from app.infra.db.predicates import LIVE
from app.infra.db.user_locks import lock_users

__all__ = ["forget_gone", "subscribe", "subscribed", "subscriptions_for", "unsubscribe"]


async def subscribe(
    session: AsyncSession, user_id: UUID, *, endpoint: str, p256dh: str, auth: str
) -> bool:
    """Store this browser as the user's, and say whether it was new to the table.

    **Upserted on the endpoint, across users**, and the keys rewritten with the
    owner: a browser that re-subscribed has new keys, and a payload encrypted
    to the old ones would not decrypt. Then the user's oldest past
    `MAX_SUBSCRIPTIONS_PER_USER` go. **Both owners are locked first**, the
    caller and whoever holds the endpoint now, in id order: two subscribes at
    once cannot both keep an eleventh, and two accounts trading browsers cannot
    deadlock on each other's rows. Does not commit.
    """
    holder = await session.scalar(
        select(PushSubscription.user_id).where(PushSubscription.endpoint == endpoint)
    )
    await lock_users(session, {user_id} if holder is None else {user_id, holder})
    result: Result[Any] = await session.execute(
        insert(PushSubscription)
        .values(user_id=user_id, endpoint=endpoint, p256dh=p256dh, auth=auth)
        .on_conflict_do_update(
            index_elements=["endpoint"],
            set_={"user_id": user_id, "p256dh": p256dh, "auth": auth},
        )
        # `xmax` is 0 only on a row this statement inserted (PostgreSQL's
        # standard way to tell an upsert's insert from its update).
        .returning(literal_column("xmax = 0"))
    )
    created = bool(result.scalar_one())
    kept = (
        select(PushSubscription.id)
        .where(PushSubscription.user_id == user_id)
        .order_by(PushSubscription.updated_at.desc(), PushSubscription.id.desc())
        .limit(MAX_SUBSCRIPTIONS_PER_USER)
    )
    await session.execute(
        delete(PushSubscription).where(
            PushSubscription.user_id == user_id, PushSubscription.id.not_in(kept)
        )
    )
    return created


async def unsubscribe(session: AsyncSession, user_id: UUID, endpoint: str) -> None:
    """Forget this browser if it is the user's. Somebody else's, or none, is a
    no-op: the answer is the same either way. Does not commit."""
    await session.execute(
        delete(PushSubscription).where(
            PushSubscription.user_id == user_id, PushSubscription.endpoint == endpoint
        )
    )


async def subscribed(session: AsyncSession, user_id: UUID, endpoint: str) -> bool:
    """Whether this browser is subscribed as this user."""
    return bool(
        await session.scalar(
            select(
                exists().where(
                    PushSubscription.user_id == user_id, PushSubscription.endpoint == endpoint
                )
            )
        )
    )


async def subscriptions_for(session: AsyncSession, user_id: UUID) -> list[PushSubscription]:
    """Every browser this user subscribed, while their account is live."""
    return list(
        (
            await session.scalars(
                select(PushSubscription)
                .join(User, User.id == PushSubscription.user_id)
                .where(PushSubscription.user_id == user_id, LIVE)
                .order_by(PushSubscription.id)
            )
        ).all()
    )


async def forget_gone(
    session: AsyncSession, ids: Sequence[UUID], outcomes: Sequence[PushOutcome]
) -> None:
    """Drop the browsers whose push service said they are gone (`ids` and
    `outcomes` pair up, as `web_push.fan_out` returns them). Does not commit."""
    gone = [row_id for row_id, done in zip(ids, outcomes, strict=True) if done is PushOutcome.GONE]
    if gone:
        await session.execute(delete(PushSubscription).where(PushSubscription.id.in_(gone)))
