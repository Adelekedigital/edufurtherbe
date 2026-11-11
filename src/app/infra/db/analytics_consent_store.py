"""A person's analytics-cookie answer (PRD 30 PR 2, decision 329).

One row per person, replaced in place, and **only ever the caller's**: every
statement names `user_id` in its `WHERE` or its insert, so nobody can read or
write another person's answer (non-negotiable 5). It is read back with the
caller, by `api/deps/core.CURRENT_USER` (`analytics_consent_of`), and
`consent_accepted` is the one rule of what an answer means inside an admin
figure's count (decision 338), never to return anyone's answer.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.db.models.legal import AnalyticsConsent

__all__ = ["consent_accepted", "record_consent"]

_COLUMNS = (AnalyticsConsent.granted, AnalyticsConsent.version, AnalyticsConsent.updated_at)


def consent_accepted(user_id: Any) -> Any:
    """`EXISTS`: ``user_id`` accepted analytics, so the vendor knows them by
    their id (decision 329); without it they are cookieless to it."""
    return (
        select(AnalyticsConsent.id)
        .where(AnalyticsConsent.user_id == user_id, AnalyticsConsent.granted.is_(True))
        .exists()
    )


async def record_consent(
    session: AsyncSession, user_id: UUID, *, granted: bool, version: str
) -> Any:
    """Record the caller's answer, replacing any earlier one. Does not commit."""
    statement = insert(AnalyticsConsent).values(user_id=user_id, granted=granted, version=version)
    return (
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[AnalyticsConsent.user_id],
                set_={
                    "granted": statement.excluded.granted,
                    "version": statement.excluded.version,
                },
            ).returning(*_COLUMNS)
        )
    ).one()
