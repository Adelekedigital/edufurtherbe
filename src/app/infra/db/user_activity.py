"""The days people used the platform: recorded once a day, counted for the
admin Overview (decision 342), by role and as a weekly trend (decision 343)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Date, and_, func, literal, select
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.domain.analytics import (
    MONTHLY_ACTIVE_DAYS,
    WEEKLY_ACTIVE_DAYS,
    ActiveUsers,
    Period,
    active_spans,
    activity_days,
    bucket_weeks,
)
from app.domain.enums import PrimaryRole
from app.infra.db.analytics_scope import counted_user
from app.infra.db.models.platform import UserActivityDay
from app.infra.db.models.user import User


async def record_active_day(session: AsyncSession, user_id: UUID, day: dt.date) -> None:
    """``user_id`` was active on ``day``; a second record of the pair is ignored."""
    await session.execute(
        insert(UserActivityDay)
        .values(user_id=user_id, day=day)
        .on_conflict_do_nothing(index_elements=["user_id", "day"])
    )


def _first_day() -> Any:
    """The first activity day recorded by anyone (the ``(day, user_id)``
    index answers it), or null with none."""
    return select(func.min(UserActivityDay.day)).scalar_subquery()


@dataclass(frozen=True, slots=True)
class ActiveSide:
    """One period's counts, by primary role."""

    #: Active in the 7 and the 30 days ending with the period's last day.
    users: dict[PrimaryRole, ActiveUsers]
    #: Weekly actives for each of the period's `bucket_weeks` spans.
    weekly: dict[PrimaryRole, list[int]]


@dataclass(frozen=True, slots=True)
class Activity:
    """What the Overview reads from the activity days."""

    now: ActiveSide
    #: The previous period, or all zeros when it is not compared (unread).
    before: ActiveSide
    earliest: dt.date | None


def _spans(period: Period) -> list[Period]:
    """The period's spans: its 7 and 30 days (`active_spans`), then each
    trend point's 7 days."""
    return [
        active_spans(period, WEEKLY_ACTIVE_DAYS)[0],
        active_spans(period, MONTHLY_ACTIVE_DAYS)[0],
        *(span for _, span in bucket_weeks(period)),
    ]


def _side_read(spans: list[Period], *columns: Any) -> Any:
    """Distinct counted people per span and `primary_role`, in one grouped
    statement whose text does not depend on how many spans there are: the
    spans' activity days go in as two date arrays (`activity_days`), and every
    span has a row, with a null role when nobody was active in it."""
    bounds = [activity_days(span) for span in spans]
    wanted = (
        func.unnest(
            literal([lo for lo, _ in bounds], ARRAY(Date)),
            literal([hi for _, hi in bounds], ARRAY(Date)),
        )
        .table_valued("lo", "hi", with_ordinality="n")
        .render_derived(with_types=False)
    )
    # An alias, so `counted_user`'s own scan of `users` cannot correlate to it.
    member = aliased(User)
    day = UserActivityDay
    return (
        select(wanted.c.n, member.primary_role, func.count(func.distinct(day.user_id)), *columns)
        .select_from(wanted)
        .outerjoin(day, and_(day.day.between(wanted.c.lo, wanted.c.hi), counted_user(day.user_id)))
        .outerjoin(member, member.id == day.user_id)
        .group_by(wanted.c.n, member.primary_role)
    )


def _side(rows: Any, spans: int) -> ActiveSide:
    counts = {role: [0] * spans for role in PrimaryRole}
    for n, role, people, *_ in rows:
        if role is not None:
            counts[PrimaryRole(role)][n - 1] = int(people)
    return ActiveSide(
        users={role: ActiveUsers(weekly=c[0], monthly=c[1]) for role, c in counts.items()},
        weekly={role: c[2:] for role, c in counts.items()},
    )


async def activity(session: AsyncSession, period: Period, compare: bool) -> Activity:
    """Everything the Overview counts from the activity days: one statement
    for the period (with the first day recorded) and, only when compared, one
    for the previous period, whatever the window's length."""
    spans = _spans(period)
    rows = (await session.execute(_side_read(spans, _first_day()))).all()
    earliest = rows[0][3] if rows else None
    before_spans = _spans(period.previous())
    before = (
        _side((await session.execute(_side_read(before_spans))).all(), len(before_spans))
        if compare
        else _side([], len(before_spans))
    )
    return Activity(now=_side(rows, len(spans)), before=before, earliest=earliest)
