"""Reading a user's own onboarding record.

Its own module beside `onboarding_writer.py` for the reason every other pair in
this package is split: a read is safe to call from anywhere and a write is not,
and putting them in one file makes that distinction a matter of remembering.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.db.models.user import UserOnboarding

__all__ = ["get_onboarding"]


async def get_onboarding(session: AsyncSession, user_id: UUID) -> Any | None:
    """This user's onboarding row, or ``None`` if they have never started.

    Null rather than a synthesised empty record: a user who has not begun is a
    different fact from one who began and got nowhere, and `last_step` cannot
    tell them apart because it is null in both.
    """
    return (
        await session.execute(
            select(UserOnboarding.completed_at, UserOnboarding.last_step).where(
                UserOnboarding.user_id == user_id
            )
        )
    ).one_or_none()
