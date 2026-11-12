"""The days people used the platform: recorded once a day, counted for the
admin Overview (decision 342)."""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.analytics import ActiveUsers, Period, activity_days
from app.infra.db.analytics_scope import counted_user
from app.infra.db.models.platform import UserActivityDay


async def record_active_day(session: AsyncSession, user_id: UUID, day: dt.date) -> None:
    """``user_id`` was active on ``day``; a second record of the pair is ignored."""
    await session.execute(
        insert(UserActivityDay)
        .values(user_id=user_id, day=day)
        .on_conflict_do_nothing(index_elements=["user_id", "day"])
    )


def active_in(span: Period) -> Any:
    """Distinct counted people with an activity day overlapping ``span``."""
    return (
        select(func.count(func.distinct(UserActivityDay.user_id)))
        .where(
            UserActivityDay.day.between(*activity_days(span)),
            counted_user(UserActivityDay.user_id),
        )
        .scalar_subquery()
    )


async def active_users(
    session: AsyncSession, weekly: tuple[Period, Period], monthly: tuple[Period, Period]
) -> tuple[ActiveUsers, ActiveUsers]:
    """Now and before, from the spans `analytics.active_spans` gives, in one
    statement."""
    w_now, w_before, m_now, m_before = (
        await session.execute(select(*(active_in(span) for span in (*weekly, *monthly))))
    ).one()
    return (
        ActiveUsers(weekly=int(w_now), monthly=int(m_now)),
        ActiveUsers(weekly=int(w_before), monthly=int(m_before)),
    )
