"""The admin Mentors page's reads: summary, tab rows and the drawer (decision 297).

Every live grant may look (`QueueViewerDep`), as on the application queue; the
actions on a mentor stay `MentorAdminDep`.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Query

from app.api.deps.core import QueueViewerDep, SessionDep
from app.api.schemas.admin_mentors import (
    AdminMentorRead,
    AdminMentorRowRead,
    AdminMentorsSummaryRead,
)
from app.api.schemas.common import StorableText
from app.core.errors import NotFoundError
from app.domain.admin_mentors import AdminMentorTab
from app.infra.db.admin_mentors import mentor_detail, mentor_rows, mentors_summary


async def admin_mentors_summary(_: QueueViewerDep, session: SessionDep) -> AdminMentorsSummaryRead:
    return AdminMentorsSummaryRead.of(await mentors_summary(session), dt.datetime.now(dt.UTC))


async def admin_mentor_rows(
    _: QueueViewerDep,
    session: SessionDep,
    tab: Annotated[AdminMentorTab, Query(description="Which tab to list.")],
    q: Annotated[
        str | None,
        Query(max_length=200, description="Part of a name or email, ignoring case."),
        StorableText,
    ] = None,
) -> list[AdminMentorRowRead]:
    now = dt.datetime.now(dt.UTC)
    rows = await mentor_rows(session, tab, now=now, q=(q or "").strip() or None)
    return [AdminMentorRowRead.from_row(row, now) for row in rows]


async def admin_mentor(user_id: UUID, _: QueueViewerDep, session: SessionDep) -> AdminMentorRead:
    now = dt.datetime.now(dt.UTC)
    row = await mentor_detail(session, user_id, now=now)
    if row is None:
        raise NotFoundError("no such mentor")
    return AdminMentorRead.from_row(row, now)


AdminMentorsSummaryDep = Annotated[AdminMentorsSummaryRead, Depends(admin_mentors_summary)]
AdminMentorRowsDep = Annotated[list[AdminMentorRowRead], Depends(admin_mentor_rows)]
AdminMentorDep = Annotated[AdminMentorRead, Depends(admin_mentor)]
