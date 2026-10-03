"""Expired idempotency keys are deleted, an hour past expiry (#353).

A stored booking response carries the mentee's own words, so a key kept past its
use is that text kept for no reason. The sweep is retention, not correctness: a
swept key must behave exactly like an expired one.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_booking import (
    URL,
    a_bookable_offering,
    a_mentee,
    body,
    first_slot,
    key,
)

from app.infra.db.engine import create_session_factory
from app.infra.db.idempotency import SWEEP_GRACE, reserve, sweep_expired_keys

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def a_key(engine: AsyncEngine, *, expires_in: dt.timedelta) -> str:
    """One stored key whose expiry is `expires_in` from now (negative: past)."""
    name = f"sweep-{uuid4()}"
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO idempotency_keys "
                "(key, endpoint, request_hash, response_body, status_code, "
                " locked_at, completed_at, expires_at) "
                "VALUES (:k, 'POST /api/v1/sessions', 'h', "
                ' CAST(\'{"booking_message": "my words"}\' AS jsonb), 201, '
                " now(), now(), now() + CAST(:d AS interval))"
            ),
            {"k": name, "d": expires_in},
        )
    return name


async def present(engine: AsyncEngine, name: str) -> bool:
    async with engine.connect() as conn:
        found = await conn.execute(
            text("SELECT 1 FROM idempotency_keys WHERE key = :k"), {"k": name}
        )
        return found.first() is not None


async def sweep(engine: AsyncEngine, **kwargs: Any) -> int:
    async with create_session_factory(engine)() as session:
        return await sweep_expired_keys(session, now=dt.datetime.now(dt.UTC), **kwargs)


async def test_only_keys_past_the_grace_are_deleted(db_engine: AsyncEngine) -> None:
    old = await a_key(db_engine, expires_in=-(SWEEP_GRACE + dt.timedelta(minutes=5)))
    just_lapsed = await a_key(db_engine, expires_in=-(SWEEP_GRACE - dt.timedelta(minutes=5)))
    live = await a_key(db_engine, expires_in=dt.timedelta(hours=20))

    deleted = await sweep(db_engine, dry_run=False)

    assert deleted >= 1
    assert not await present(db_engine, old), "a key an hour past expiry was kept"
    assert await present(db_engine, just_lapsed), "a key inside the grace was swept"
    assert await present(db_engine, live), "a live key was swept"


async def test_a_dry_run_counts_and_deletes_nothing(db_engine: AsyncEngine) -> None:
    old = await a_key(db_engine, expires_in=-dt.timedelta(days=3))

    counted = await sweep(db_engine, dry_run=True)

    assert counted >= 1
    assert await present(db_engine, old)


async def test_a_backlog_larger_than_one_batch_is_cleared(db_engine: AsyncEngine) -> None:
    names = [await a_key(db_engine, expires_in=-dt.timedelta(days=2)) for _ in range(7)]

    deleted = await sweep(db_engine, dry_run=False, batch=3)

    assert deleted >= 7
    assert not [n for n in names if await present(db_engine, n)]


async def test_a_reclaim_in_flight_survives_the_sweep(db_engine: AsyncEngine) -> None:
    """The race the outer `WHERE` exists for.

    A client reclaims an expired key (moving `expires_at` forward) and has not
    committed when the sweep reaches the row. The sweep waits on the row lock,
    then re-checks expiry against the reclaimed version and leaves it alone.
    """
    name = await a_key(db_engine, expires_in=-dt.timedelta(days=2))
    factory = create_session_factory(db_engine)
    async with factory() as reclaimer:
        held = await reserve(
            reclaimer,
            key=name,
            user_id=None,  # type: ignore[arg-type]  # the fixture rows are anonymous
            endpoint="POST /api/v1/sessions",
            request_hash="h2",
        )
        assert type(held).__name__ == "Held"

        sweeping = asyncio.create_task(sweep(db_engine, dry_run=False))
        await asyncio.sleep(0.5)
        assert not sweeping.done(), "the sweep did not wait for the reclaim's lock"
        await reclaimer.commit()
        await sweeping

    assert await present(db_engine, name), "the sweep deleted a key a client had just reclaimed"


async def test_a_swept_key_books_afresh_like_an_expired_one(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, session_type = await a_bookable_offering(db_engine, "sweep-rebook")
    other, other_type = await a_bookable_offering(db_engine, "sweep-rebook-2")
    _, headers = await a_mentee(db_engine, "sweep-rebook")
    one = await first_slot(api_client, mentor, session_type)
    slots = await api_client.get(
        f"/api/v1/users/{other}/availability/slots",
        params={"session_type_id": str(other_type)},
    )
    two = str(slots.json()["data"][2]["start"])
    same = key()

    first = await api_client.post(URL, json=body(session_type, one), headers=headers | same)
    assert first.status_code == 201, first.text
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE idempotency_keys SET expires_at = now() - interval '2 hours' WHERE key = :k"
            ),
            {"k": same["Idempotency-Key"]},
        )
    await sweep(db_engine, dry_run=False)
    after = await api_client.post(URL, json=body(other_type, two), headers=headers | same)

    assert after.status_code == 201, after.text
    assert after.json()["id"] != first.json()["id"]
    assert "Idempotent-Replayed" not in after.headers


async def test_the_daily_retention_job_reports_the_keys_it_deleted(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It rides `sweep-intake-files`, and runs even with no storage configured."""
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
    old = await a_key(db_engine, expires_in=-dt.timedelta(days=2))

    counts = await runner.RuntimeJobs(settings=get_settings())._sweep_intake_files(dry_run=False)

    assert counts["idempotency_keys_deleted"] >= 1
    assert counts["purged"] == 0
    assert not await present(db_engine, old)
