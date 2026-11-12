"""One PostHog dashboard sync run (decisions 341 and 344): the internal ids
read from this database, then `sync`, off the event loop. The daily
`sync-posthog-dashboards` job and `scripts/sync_posthog_dashboards.py` both
call `sync_dashboards`, so the two cannot drift."""

from __future__ import annotations

import asyncio
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.infra.clients.posthog_admin import PostHogAdmin
from app.infra.db.analytics_scope import internal_ids
from app.infra.db.engine import create_database_engine
from app.infra.posthog_dashboards.sync import Plan, sync

__all__ = ["Plan", "read_internal", "sync_dashboards"]


async def read_internal(settings: Settings) -> list[UUID]:
    """The internal accounts' ids, on an engine of the run's own."""
    engine = create_database_engine(settings)
    try:
        async with AsyncSession(engine) as session:
            return await internal_ids(session)
    finally:
        await engine.dispose()


async def sync_dashboards(settings: Settings, admin: PostHogAdmin, *, apply: bool) -> Plan:
    """Plan, and with ``apply`` make, every change with the ids read now.
    The sync's HTTP is synchronous, so it runs in a worker thread (#370)."""
    internal = await read_internal(settings)
    return await asyncio.to_thread(sync, admin, internal, apply=apply)
