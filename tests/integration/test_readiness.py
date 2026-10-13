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
