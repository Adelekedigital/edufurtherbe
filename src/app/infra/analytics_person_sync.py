"""The one-off PostHog person sync (decision 340): run by
`scripts/sync_person_properties.py`, which only wires it up.

Persons the history backfill created before decision 340 have no properties,
and a rerun cannot add them: PostHog drops its repeated `uuid`s, `$set` and
all. So every sendable person (`analytics_scope.sendable`: live, not internal)
**whom PostHog already knows** (`AnalyticsReader.known_persons`) gets one
`$set` event (`analytics_events.person_update`) with the properties the drain
sends, read by the same statement (`analytics_persons`), a page of `BATCH` per
request. Nobody PostHog has never seen is sent, so the sync creates no person
(data minimisation): their first event will describe them. `$set`, `$set_once`
and `$unset` are safe to repeat, so a rerun or a stopped run needs no cursor.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.domain.analytics_events import person_update
from app.domain.ports import AnalyticsReader, AnalyticsSink
from app.infra.db.analytics_persons import sendable_persons
from app.infra.db.analytics_scope import internal_ids

__all__ = ["BATCH", "SyncReport", "sync_person_properties"]

#: People per page: one database read, one PostHog read and one request.
BATCH = 100


@dataclass
class SyncReport:
    """Sendable people seen, those PostHog knows (the ones to send), how many
    were sent, and the first update built."""

    sendable: int = 0
    known: int = 0
    sent: int = 0
    sample: dict[str, Any] | None = None


async def sync_person_properties(
    engine: AsyncEngine,
    *,
    reader: AnalyticsReader,
    sink: AnalyticsSink | None,
    report: SyncReport | None = None,
    batch: int = BATCH,
) -> SyncReport:
    """One person update per sendable person PostHog knows, ``batch`` to a
    request; with no ``sink``, a dry run that counts them. ``report`` is
    filled as it goes, so a caller holding it knows what was sent before a
    failure."""
    report = report if report is not None else SyncReport()
    after: UUID | None = None
    async with AsyncSession(engine) as session:
        # Every PostHog read leaves the internal accounts out (decision 331).
        internal = await internal_ids(session)
        while True:
            page = await sendable_persons(session, after=after, limit=batch)
            known = await asyncio.to_thread(reader.known_persons, list(page), internal)
            updates = [person_update(user, page[user]) for user in page if user in known]
            report.sendable += len(page)
            report.known += len(updates)
            if updates and report.sample is None:
                report.sample = updates[0]
            if sink is not None and updates:
                await asyncio.to_thread(sink.capture, updates)
                report.sent += len(updates)
            if len(page) < batch:
                return report
            after = list(page)[-1]
