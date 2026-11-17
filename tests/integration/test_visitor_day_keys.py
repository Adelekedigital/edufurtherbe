"""Each UTC day's random visitor key, and its deletion once the day is over
(decision 348).

A visitor key is an HMAC under the day's key, so once that key is gone no
stored `visitor_key` of the day can be recomputed from a person.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import make_public_mentor, token_for

from app.core.config import Settings
from app.domain.activity import activity_day
from app.infra.db.engine import create_session_factory
from app.infra.db.visitor_day_keys import day_key, prune_past_keys
from conftest import bearer, build_api_app, client_for

pytestmark = [pytest.mark.db, pytest.mark.anyio]

DAY = dt.date(2026, 11, 23)
BROWSER = {"User-Agent": "Mozilla/5.0 Chrome/131", "X-Forwarded-For": "203.0.113.7"}


async def keys(engine: AsyncEngine) -> dict[dt.date, bytes]:
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT day, key FROM visitor_day_keys"))
        return {row.day: bytes(row.key) for row in result}


async def test_a_new_day_draws_a_new_random_key_and_deletes_the_last(
    db_engine: AsyncEngine,
) -> None:
    factory = create_session_factory(db_engine)

    async with factory() as session:
        first = await day_key(session, DAY)
    async with factory() as session:
        again = await day_key(session, DAY)
        nextday = await day_key(session, DAY + dt.timedelta(days=1))

    assert first is not None
    assert len(first) == 32
    assert again == first
    assert nextday is not None
    assert nextday != first
    assert await keys(db_engine) == {DAY + dt.timedelta(days=1): nextday}


async def test_a_day_another_process_has_ended_gets_no_key(db_engine: AsyncEngine) -> None:
    """A request that computed its day just before midnight, after another
    process drew the next day's key, records nothing: no error, and the ended
    day's key is neither returned nor drawn again."""
    factory = create_session_factory(db_engine)
    async with factory() as session:
        await day_key(session, DAY)
    async with factory() as session:
        nextday = await day_key(session, DAY + dt.timedelta(days=1))

    async with factory() as session:
        late = await day_key(session, DAY)

    assert late is None
    assert await keys(db_engine) == {DAY + dt.timedelta(days=1): nextday}


async def test_an_ended_day_still_in_the_table_is_not_served(db_engine: AsyncEngine) -> None:
    """Between a later day's insert and the prune, the earlier row is ignored."""
    async with db_engine.begin() as conn:
        for day in (DAY, DAY + dt.timedelta(days=1)):
            await conn.execute(
                text("INSERT INTO visitor_day_keys (day, key) VALUES (:day, :key)"),
                {"day": day, "key": bytes(32)},
            )
    factory = create_session_factory(db_engine)

    async with factory() as session:
        assert await day_key(session, DAY) is None


@pytest.mark.isolated_db
async def test_two_processes_asking_at_once_share_one_key(db_engine: AsyncEngine) -> None:
    """Eight sessions on their own connections race the first insert of a day."""
    factory = create_session_factory(db_engine)

    async def ask() -> bytes:
        async with factory() as session:
            return await day_key(session, DAY)

    drawn = await asyncio.gather(*(ask() for _ in range(8)))

    assert len(set(drawn)) == 1
    assert await keys(db_engine) == {DAY: drawn[0]}


async def test_the_prune_deletes_only_days_before_today(db_engine: AsyncEngine) -> None:
    yesterday = DAY - dt.timedelta(days=1)
    tomorrow = DAY + dt.timedelta(days=1)  # drawn by a process whose clock is ahead
    async with db_engine.begin() as conn:
        for day in (yesterday - dt.timedelta(days=1), yesterday, DAY, tomorrow):
            await conn.execute(
                text("INSERT INTO visitor_day_keys (day, key) VALUES (:day, :key)"),
                {"day": day, "key": bytes(32)},
            )
    factory = create_session_factory(db_engine)

    async with factory() as session:
        counted = await prune_past_keys(session, today=DAY, dry_run=True)
    assert counted == 2
    assert len(await keys(db_engine)) == 4

    async with factory() as session:
        deleted = await prune_past_keys(session, today=DAY, dry_run=False)
        await session.commit()
    assert deleted == 2
    assert set(await keys(db_engine)) == {DAY, tomorrow}


async def test_the_daily_retention_job_deletes_yesterdays_key(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no visit since midnight, yesterday's key still goes by the next run."""
    from app.core.config import get_settings
    from app.infra.jobs import runner

    class Borrowed:
        """The test engine, lent to the job without letting it dispose it."""

        def __getattr__(self, name: str) -> Any:
            return getattr(db_engine, name)

        async def dispose(self) -> None:
            return None

    monkeypatch.setattr(runner, "create_database_engine", lambda _s: Borrowed())
    monkeypatch.setattr(
        runner, "create_session_factory", lambda _e: create_session_factory(db_engine)
    )
    monkeypatch.setattr(runner, "intake_storage_for", lambda _s, _c: None)
    today = activity_day(dt.datetime.now(dt.UTC))
    factory = create_session_factory(db_engine)
    async with factory() as session:
        await day_key(session, today - dt.timedelta(days=1))

    jobs = runner.RuntimeJobs(settings=get_settings())
    dry = await jobs._sweep_intake_files(dry_run=True)
    assert dry["visitor_day_keys_deleted"] == 1
    assert set(await keys(db_engine)) == {today - dt.timedelta(days=1)}

    counts = await jobs._sweep_intake_files(dry_run=False)

    assert counts["visitor_day_keys_deleted"] == 1
    assert await keys(db_engine) == {}


async def test_two_app_processes_count_one_visitor_once(db_engine: AsyncEngine) -> None:
    """Two apps share nothing in memory; the database's day key makes them agree."""
    mentor = await make_public_mentor(db_engine, "two-processes")

    for _ in range(2):
        app = build_api_app(db_engine, None, Settings(_env_file=None))
        async with client_for(app) as client:
            response = await client.post(
                f"/api/v1/mentors/{mentor}/visits", json={}, headers=BROWSER
            )
            assert response.status_code == 204

    async with db_engine.connect() as conn:
        visits = (await conn.execute(text("SELECT count(*) FROM mentor_profile_visits"))).scalar()
    assert visits == 1
    assert len(await keys(db_engine)) == 1


async def test_a_visit_that_stores_nothing_still_keeps_the_day_key(
    db_engine: AsyncEngine,
) -> None:
    """The key is committed on its own, so a skipped visit still leaves the
    key every other process reads."""
    mentor = await make_public_mentor(db_engine, "own-key")
    own = bearer(await token_for(db_engine, mentor))
    app = build_api_app(db_engine, None, Settings(_env_file=None))

    async with client_for(app) as client:
        response = await client.post(f"/api/v1/mentors/{mentor}/visits", json={}, headers=own)

    assert response.status_code == 204
    today = activity_day(dt.datetime.now(dt.UTC))
    assert set(await keys(db_engine)) == {today}


async def test_a_visit_on_a_day_another_process_ended_is_a_silent_204(
    db_engine: AsyncEngine,
) -> None:
    """Never a `500`: the event is dropped like any other that is not counted."""
    mentor = await make_public_mentor(db_engine, "ended-day")
    tomorrow = activity_day(dt.datetime.now(dt.UTC)) + dt.timedelta(days=1)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO visitor_day_keys (day, key) VALUES (:day, :key)"),
            {"day": tomorrow, "key": bytes(32)},
        )
    app = build_api_app(db_engine, None, Settings(_env_file=None))

    async with client_for(app) as client:
        visit = await client.post(f"/api/v1/mentors/{mentor}/visits", json={}, headers=BROWSER)
        share = await client.post(
            f"/api/v1/mentors/{mentor}/shares", json={"method": "copy"}, headers=BROWSER
        )

    assert (visit.status_code, share.status_code) == (204, 204)
    async with db_engine.connect() as conn:
        stored = await conn.execute(
            text(
                "SELECT (SELECT count(*) FROM mentor_profile_visits)"
                " + (SELECT count(*) FROM mentor_profile_shares)"
            )
        )
        assert stored.scalar() == 0
    assert set(await keys(db_engine)) == {tomorrow}
