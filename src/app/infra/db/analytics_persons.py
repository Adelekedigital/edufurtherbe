"""PostHog person properties (decision 340): one read per batch.

Server events would otherwise create bare persons. Before a batch leaves, the
drain and the history backfill both pass it through `with_person_properties`,
which reads every person in it in **one statement** and merges
`analytics_events.person_properties` into each of their events. The one-off
sync (`sendable_persons`) reads the same statement a page at a time.

**Only a sendable person is described** (`analytics_scope.sendable`): one
deleted or made internal after their event was queued is sent bare, as the
drain always sent it, never with properties read from a row that should no
longer speak for them. The purge drops their unsent events anyway
(`forget_unsent_analytics`).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.analytics_events import person_properties, with_person
from app.infra.db.analytics_scope import sendable
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.user import User
from app.infra.db.predicates import migrated

__all__ = ["sendable_persons", "with_person_properties"]


async def _persons(
    session: AsyncSession, *criteria: Any, limit: int | None = None
) -> dict[UUID, dict[str, Any]]:
    """Each sendable person matching ``criteria``, by id, with their properties."""
    rows = await session.execute(
        select(
            User.id,
            User.primary_role,
            migrated(User).label("is_migrated"),
            User.signup_source,
            User.signup_channel,
            User.created_at,
            MentorProfile.approval_status,
            MentorProfile.listing_status,
        )
        .outerjoin(
            MentorProfile,
            and_(MentorProfile.user_id == User.id, MentorProfile.deleted_at.is_(None)),
        )
        .where(sendable(User), *criteria)
        .order_by(User.id)
        .limit(limit)
    )
    return {
        row.id: person_properties(
            user_id=row.id,
            role=row.primary_role,
            is_migrated=row.is_migrated,
            signup_source=row.signup_source,
            signup_channel=row.signup_channel,
            created_at=row.created_at,
            approval_status=row.approval_status,
            listing_status=row.listing_status,
        )
        for row in rows
    }


async def with_person_properties(
    session: AsyncSession, events: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """``events`` with their person's `$set`/`$set_once`, read in one statement."""
    if not events:
        return []
    ids = {UUID(event["distinct_id"]) for event in events}
    persons = {
        str(key): value for key, value in (await _persons(session, User.id.in_(ids))).items()
    }
    return [
        with_person(event, persons[event["distinct_id"]])
        if event["distinct_id"] in persons
        else dict(event)
        for event in events
    ]


async def sendable_persons(
    session: AsyncSession, *, after: UUID | None, limit: int
) -> dict[UUID, dict[str, Any]]:
    """The next ``limit`` sendable persons by id after ``after``, with their
    properties: one statement per page, for the one-off sync."""
    return await _persons(session, *([User.id > after] if after else []), limit=limit)
