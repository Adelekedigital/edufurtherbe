"""Who the admin Analytics numbers leave out, said once (PRD 31, decision 324).

Every analytics statement composes `counted_user` or `counted_session`, so the
tabs cannot disagree about who is excluded. Left out: anybody holding a live
admin grant (`live_grant`) and the dev demo accounts (`DEMO_EMAIL_LIKE`).
Preview sessions need nothing here: a preview writes nothing (decision 313).
PRD 30's `is_internal` joins `_excluded_users` when it ships, and nowhere else.

A deleted account is **counted**: what it did happened.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import select, union
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.mentor_stats import Window
from app.infra.db.demo_seed import DEMO_EMAIL_LIKE
from app.infra.db.models.admin import AdminUser
from app.infra.db.models.sessions import Session
from app.infra.db.models.user import User
from app.infra.db.predicates import live_grant


def _excluded_users() -> Any:
    """The ids no analytics figure counts: live admin grants (`live_grant`,
    the rule `is_live_admin` reads) and demo accounts. A union of two cheap
    scans rather than `users` filtered by a correlated `EXISTS`: every scalar
    subquery of a tab evaluates it once, so its cost is paid ~100 times."""
    return union(
        select(AdminUser.user_id).where(live_grant()),
        select(User.id).where(User.email.like(DEMO_EMAIL_LIKE)),
    )


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
