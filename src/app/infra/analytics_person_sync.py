"""The one-off PostHog person sync (decision 340): run by
`scripts/sync_person_properties.py`, which only wires it up.

Persons the history backfill created before decision 340 have no properties,
and a rerun cannot add them: PostHog drops its repeated `uuid`s, `$set` and
all. So every sendable person (`analytics_scope.sendable`: live, not internal)
gets one `$set` event (`analytics_events.person_update`) with the properties
the drain sends, read by the same statement (`analytics_persons`), a page of
`BATCH` per request. `$set` and `$set_once` are safe to repeat, so a rerun or
a stopped run needs no cursor.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.domain.analytics_events import person_update
from app.domain.ports import AnalyticsSink
from app.infra.db.analytics_persons import sendable_persons

__all__ = ["BATCH", "SyncReport", "sync_person_properties"]

#: Updates per request.
BATCH = 100


@dataclass
class SyncReport:
    """How many updates were built and sent, and the first one built."""

    built: int = 0
    sent: int = 0
    sample: dict[str, Any] | None = None


async def sync_person_properties(
    engine: AsyncEngine, *, sink: AnalyticsSink | None, batch: int = BATCH
) -> SyncReport:
    """One person update per sendable person, ``batch`` to a request; with no
    ``sink``, a dry run that builds and counts them."""
    report = SyncReport()
    after: UUID | None = None
    async with AsyncSession(engine) as session:
        while True:
            page = await sendable_persons(session, after=after, limit=batch)
            updates = [person_update(user_id, person) for user_id, person in page.items()]
            if updates and report.sample is None:
                report.sample = updates[0]
            report.built += len(updates)
            if sink is not None and updates:
                await asyncio.to_thread(sink.capture, updates)
                report.sent += len(updates)
            if len(page) < batch:
                return report
            after = list(page)[-1]
