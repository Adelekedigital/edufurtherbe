"""Who the admin Analytics numbers leave out, said once (PRD 31, decision 324).

Every analytics statement composes `counted_user` or `counted_session`, so the
tabs cannot disagree about who is excluded. Left out: internal accounts,
`users.is_internal` (PRD 30, decision 329), which the database keeps as a live
admin grant or a demo address (`DEMO_EMAIL_LIKE`). Preview sessions need
nothing here: a preview writes nothing (decision 313).

A deleted account is **counted**: what it did happened.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.mentor_stats import Window
from app.infra.db.models.sessions import Session
from app.infra.db.models.user import User


def _excluded_users() -> Any:
    """The ids no analytics figure counts: the internal accounts. One flag,
    so a revoked grant and a new demo account are right without a second
    rule here; the union of grants and demo addresses it replaced is the
    database's `internal_account()` now."""
    return select(User.id).where(User.is_internal)


async def internal_ids(session: AsyncSession) -> list[UUID]:
    """The internal accounts' ids, for a reader that cannot join this
    database (PostHog knows them as distinct ids): the same rule, read out."""
    return list((await session.scalars(_excluded_users())).all())


def counted_user(user_id: Any) -> Any:
    """``user_id`` is somebody the analytics count. `users.id` is never null,
    so `NOT IN` is safe here."""
    return user_id.not_in(_excluded_users())


def counted_session() -> Any:
    """A session both of whose people are counted."""
    excluded = _excluded_users()
    return Session.mentor_id.not_in(excluded) & Session.mentee_id.not_in(excluded)


Measure = Callable[[Window], Any]


async def per_window_each(
    session: AsyncSession, windows: Sequence[Window], *measures: Measure
) -> list[list[Any]]:
    """Every measure (a window to a scalar subquery) over every window, in one
    statement and one snapshot: ``result[i][j]`` is ``measures[i]`` over
    ``windows[j]``. Fewer statements is most of a tab's speed."""
    row = (
        await session.execute(select(*(m(window) for m in measures for window in windows)))
    ).one()
    width = len(windows)
    return [list(row[i * width : (i + 1) * width]) for i in range(len(measures))]


async def per_window(
    session: AsyncSession, windows: Sequence[Window], measure: Measure
) -> list[Any]:
    """`per_window_each` for one measure."""
    return (await per_window_each(session, windows, measure))[0]
