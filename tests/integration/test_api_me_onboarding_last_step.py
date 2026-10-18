"""`/me` → `onboarding.last_step`: where the caller got to (decision 268).

The onboarding screen tells a migrated mentee from one who came back mid-way
by it. Only the flow's own steps are published; a legacy value the ETL wrote
reads as null, the same as never having started.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_onboarding_completion import seed

from app.domain.onboarding import STEPS
from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def last_step_on_me(client: httpx.AsyncClient, auth_id: UUID) -> object:
    me = (await client.get("/api/v1/me", headers=bearer(api_token(auth_id)))).json()
    return me["onboarding"]["last_step"]


async def test_never_started_is_null(db_engine: AsyncEngine, api_client: httpx.AsyncClient) -> None:
    auth_id = uuid4()
    await seed(db_engine, auth_id, profile=False, goal=False)

    assert await last_step_on_me(api_client, auth_id) is None


@pytest.mark.parametrize("step", STEPS)
async def test_a_saved_step_is_read_back(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient, step: str
) -> None:
    auth_id = uuid4()
    await seed(db_engine, auth_id)
    await api_client.put(
        "/api/v1/me/onboarding", json={"last_step": step}, headers=bearer(api_token(auth_id))
    )

    assert await last_step_on_me(api_client, auth_id) == step


async def test_a_legacy_step_reads_as_null(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """The ETL wrote Bubble's own step values, such as "6"; they are not this
    flow's steps, so a migrated mentee reads as never having started here."""
    auth_id = uuid4()
    user_id = await seed(db_engine, auth_id)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO user_onboarding (user_id, last_step) VALUES (:u, '6')"),
            {"u": user_id},
        )

    assert await last_step_on_me(api_client, auth_id) is None
