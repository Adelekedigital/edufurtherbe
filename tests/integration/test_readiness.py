"""`/ready` reports whether the database answers; `/health` never asks it."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings
from app.infra.db.engine import create_session_factory
from app.main import create_app
from conftest import client_for

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def test_ready_when_the_database_answers(api_client: httpx.AsyncClient) -> None:
    response = await api_client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok"}}


async def test_not_ready_and_says_nothing_more_when_it_does_not() -> None:
    """A closed port refuses at once: the answer is a 503 naming the check,
    never the host, the driver or the error."""
    app = create_app(Settings(_env_file=None))
    dead = create_async_engine("postgresql+asyncpg://u:secret@127.0.0.1:1/none")
    app.state.session_factory = create_session_factory(dead)
    try:
        async with client_for(app) as client:
            ready = await client.get("/ready")
            alive = await client.get("/health")
    finally:
        await dead.dispose()

    assert ready.status_code == 503
    assert ready.json() == {"status": "not ready", "checks": {"database": "unreachable"}}
    assert "127.0.0.1" not in ready.text
    assert "secret" not in ready.text
    assert alive.status_code == 200


async def test_a_database_that_does_not_answer_is_not_ready_within_the_timeout(
    api_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accepting the connection and never answering is the case the timeout is
    for: a refused port fails at once and proves nothing about it."""
    import asyncio
    import time

    from app.api.deps import core

    monkeypatch.setattr(core, "READY_TIMEOUT", 0.2)
    real_text = core.text
    monkeypatch.setattr(core, "text", lambda _sql: real_text("SELECT pg_sleep(5)"))

    started = time.monotonic()
    response = await asyncio.wait_for(api_client.get("/ready"), timeout=3)

    assert response.status_code == 503
    assert time.monotonic() - started < 2
