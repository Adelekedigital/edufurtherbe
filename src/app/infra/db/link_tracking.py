"""The writes behind a mentor profile link's visits and shares (decision 346).

Insert only, and a repeat is a no-op: the dedupe is each table's `UNIQUE`
key, so two requests racing for the same visitor and day store one row.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import LinkVia, ShareMethod
from app.infra.db.models.platform import MentorProfileShare, MentorProfileVisit


async def record_visit(
    session: AsyncSession,
    *,
    mentor_id: UUID,
    day: dt.date,
    visitor_key: bytes,
    signed_in: bool,
    via: LinkVia | None,
    referrer_host: str | None,
) -> None:
    """A visitor's first look at ``mentor_id`` on ``day``; a repeat is ignored,
    keeping the first visit's ``via`` and ``referrer_host``."""
    await session.execute(
        insert(MentorProfileVisit)
        .values(
            mentor_user_id=mentor_id,
            day=day,
            visitor_key=visitor_key,
            signed_in=signed_in,
            via=via,
            referrer_host=referrer_host,
        )
        .on_conflict_do_nothing(index_elements=["mentor_user_id", "day", "visitor_key"])
    )


async def record_share(
    session: AsyncSession,
    *,
    mentor_id: UUID,
    day: dt.date,
    visitor_key: bytes,
    signed_in: bool,
    method: ShareMethod,
    own: bool,
) -> None:
    """One person sharing ``mentor_id``'s link by ``method`` on ``day``; a
    repeat the same way that day is ignored."""
    await session.execute(
        insert(MentorProfileShare)
        .values(
            mentor_user_id=mentor_id,
            day=day,
            visitor_key=visitor_key,
            signed_in=signed_in,
            method=method,
            own=own,
        )
        .on_conflict_do_nothing(index_elements=["mentor_user_id", "day", "visitor_key", "method"])
    )
