"""One PostHog dashboard sync at a time, on Postgres (decision 344): a run
that finds the lock held writes nothing, and a free run reads the internal
accounts through the session it is given."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.posthog_fakes import FakePostHog

from app.infra.db.engine import create_session_factory
from app.infra.posthog_dashboards.run import SYNC_LOCK, sync_dashboards
from conftest import add_user

pytestmark = [pytest.mark.db, pytest.mark.asyncio, pytest.mark.isolated_db]


async def test_a_run_beside_a_running_one_writes_nothing_and_the_next_one_runs(
    db_engine: AsyncEngine,
) -> None:
    async with db_engine.begin() as conn:
        demo = await add_user(conn, "lock-test@demo.edufurther.test")
        await add_user(conn, "member@example.com")
    posthog = FakePostHog()
    factory = create_session_factory(db_engine)

    async with db_engine.connect() as running, running.begin():
        await running.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": SYNC_LOCK})

        assert await sync_dashboards(factory, posthog.admin(), apply=True) is None
        assert posthog.writes == []

    plan = await sync_dashboards(factory, posthog.admin(), apply=True)

    assert plan is not None
    assert plan.internal == 1
    assert len(posthog.rows["insights"]) == plan.counts()["created"] - 5
    exclusion = posthog.rows["insights"][0]["query"]["source"]["properties"][0]["key"]
    assert str(demo) in exclusion
