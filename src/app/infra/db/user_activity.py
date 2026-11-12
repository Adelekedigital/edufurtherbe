"""The days people used the platform: recorded once a day, counted for the
admin Overview (decision 342), by role and as a weekly trend (decision 343)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.analytics import (
    MONTHLY_ACTIVE_DAYS,
    WEEKLY_ACTIVE_DAYS,
    ActiveUsers,
    CountsByGroup,
    Period,
    active_spans,
    activity_days,
    bucket_weeks,
)
from app.domain.enums import PrimaryRole
from app.infra.db.analytics_scope import counted_user
from app.infra.db.models.platform import UserActivityDay
from app.infra.db.models.user import User
from app.infra.db.predicates import has_primary_role


async def record_active_day(session: AsyncSession, user_id: UUID, day: dt.date) -> None:
    """``user_id`` was active on ``day``; a second record of the pair is ignored."""
    await session.execute(
        insert(UserActivityDay)
        .values(user_id=user_id, day=day)
        .on_conflict_do_nothing(index_elements=["user_id", "day"])
    )


def active_in(span: Period, role: PrimaryRole | None = None) -> Any:
    """Distinct counted people with an activity day overlapping ``span``;
    with ``role``, only that `primary_role` (the Growth tab's split)."""
    where = [
        UserActivityDay.day.between(*activity_days(span)),
        counted_user(UserActivityDay.user_id),
    ]
    if role is not None:
        # A subquery, not a join: a join would let `counted_user`'s own scan
        # of `users` correlate to the joined row.
        where.append(UserActivityDay.user_id.in_(select(User.id).where(has_primary_role(role))))
    return (
        select(func.count(func.distinct(UserActivityDay.user_id))).where(*where).scalar_subquery()
    )


def _first_day() -> Any:
    """The first activity day recorded by anyone (the ``(day, user_id)``
    index answers it), or null with none."""
    return select(func.min(UserActivityDay.day)).scalar_subquery()


#: Everybody, then each primary role.
_GROUPS: tuple[PrimaryRole | None, ...] = (None, *PrimaryRole)


@dataclass(frozen=True, slots=True)
class Activity:
    """What the Overview reads from the activity days."""

    #: Now and before, for everybody (``None``) and for each primary role.
    users: dict[PrimaryRole | None, tuple[ActiveUsers, ActiveUsers]]
    #: Weekly actives per group for each `bucket_weeks` span of the period
    #: and of the previous one.
    weekly_now: CountsByGroup
    weekly_before: CountsByGroup
    earliest: dt.date | None


async def activity(session: AsyncSession, period: Period) -> Activity:
    """Everything the Overview counts from the activity days, in one
    statement: per group the `active_spans` four and the trend's spans (now,
    then before), and the first day recorded."""
    sides = (period, period.previous())
    spans = [
        *active_spans(period, WEEKLY_ACTIVE_DAYS),
        *active_spans(period, MONTHLY_ACTIVE_DAYS),
        *(span for side in sides for _, span in bucket_weeks(side)),
    ]
    row = (
        await session.execute(
            select(_first_day(), *(active_in(span, role) for role in _GROUPS for span in spans))
        )
    ).one()
    counts = [int(n) for n in row[1:]]
    points = (len(spans) - 4) // 2
    users: dict[PrimaryRole | None, tuple[ActiveUsers, ActiveUsers]] = {}
    weekly_now: dict[PrimaryRole | None, list[int]] = {}
    weekly_before: dict[PrimaryRole | None, list[int]] = {}
    for i, role in enumerate(_GROUPS):
        w_now, w_before, m_now, m_before, *trend = counts[i * len(spans) : (i + 1) * len(spans)]
        users[role] = (
            ActiveUsers(weekly=w_now, monthly=m_now),
            ActiveUsers(weekly=w_before, monthly=m_before),
        )
        weekly_now[role], weekly_before[role] = trend[:points], trend[points:]
    return Activity(
        users=users, weekly_now=weekly_now, weekly_before=weekly_before, earliest=row[0]
    )
