"""Reading a user's own invites."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.db.models.referrals import Referral, ReferralUnlock

__all__ = ["is_unlocked", "list_referrals"]


async def list_referrals(session: AsyncSession, referrer_id: UUID) -> Sequence[Any]:
    """This user's invites, newest first.

    Ordered to match ``ix_referrals_referrer_invited``, which is
    ``(referrer_id, invited_at DESC)`` — the index exists for this query and
    reversing the sort here would silently stop using it.
    """
    return (
        await session.execute(
            select(
                Referral.id,
                Referral.code,
                Referral.invitee_email,
                Referral.invited_at,
                Referral.signed_up_at,
                Referral.qualified_at,
            )
            .where(Referral.referrer_id == referrer_id)
            .order_by(Referral.invited_at.desc())
        )
    ).all()


async def is_unlocked(session: AsyncSession, user_id: UUID) -> bool:
    """Whether this user has opened the recurring grant.

    One indexed lookup against ``uq_referral_unlocks_user_id``, which is the
    reason the unlock is its own table rather than an aggregate over
    ``referrals``: the monthly job asks this for every mentee.
    """
    return (
        await session.scalar(select(ReferralUnlock.id).where(ReferralUnlock.user_id == user_id))
    ) is not None
