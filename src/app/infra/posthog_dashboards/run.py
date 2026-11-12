"""One PostHog dashboard sync run (decisions 341 and 344): the internal ids
read from this database, then `sync`, off the event loop, under one lock. The
daily `sync-posthog-dashboards` job and `scripts/sync_posthog_dashboards.py`
both call `sync_dashboards`, so the two cannot drift."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.clients.posthog_admin import PostHogAdmin
from app.infra.db.analytics_scope import internal_ids
from app.infra.posthog_dashboards.sync import Plan, sync

__all__ = ["SYNC_LOCK", "Plan", "read_internal", "sync_dashboards", "take_lock"]

#: The advisory lock one sync holds from start to end, so a QStash redelivery
#: or the script cannot run beside it and create a second set. A fixed key:
#: there is one PostHog project. Any constant no other lock uses.
SYNC_LOCK = 0x50484442  # "PHDB"


async def take_lock(session: AsyncSession) -> bool:
    """The sync lock for this transaction, or ``False`` when a run holds it."""
    return bool(await session.scalar(select(func.pg_try_advisory_xact_lock(SYNC_LOCK))))


async def read_internal(session: AsyncSession) -> list[UUID]:
    return await internal_ids(session)


async def _to_the_end(work: Callable[[], Plan]) -> Plan:
    """``work`` in a worker thread (#370), awaited to its end even when the
    caller is cancelled: a thread cannot be stopped, so releasing the lock
    early would let a second run write beside it. The cancellation is then
    raised."""
    running = asyncio.ensure_future(asyncio.to_thread(work))
    cancelled = False
    while not running.done():
        try:
            await asyncio.shield(running)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return running.result()


async def sync_dashboards(
    factory: Callable[[], Any], admin: PostHogAdmin, *, apply: bool
) -> Plan | None:
    """Plan, and with ``apply`` make, every change with the ids read now;
    ``None`` when another run holds the lock. One transaction holds the lock
    for the whole run and writes nothing to this database."""
    session: AsyncSession
    async with factory() as session, session.begin():
        if not await take_lock(session):
            return None
        internal = await read_internal(session)
        return await _to_the_end(lambda: sync(admin, internal, apply=apply))
