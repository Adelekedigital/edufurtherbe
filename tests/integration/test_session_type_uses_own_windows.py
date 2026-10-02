"""The owner's read says whether an offering books into its own windows (#199).

Frontend request #146: the Calendar keeps weekly hours at least as long as the
shortest offering that books into them, and needs to leave out offerings that
book into their own dedicated windows instead.
"""

from __future__ import annotations

from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_me_session_type_writes import URL, as_mentor, body
from tests.integration.test_session_type_windows_approval import own, window

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def offering_with_window(
    client: httpx.AsyncClient, engine: AsyncEngine, tag: str
) -> tuple[UUID, str, str]:
    _, auth = await as_mentor(engine, tag)
    headers = bearer(api_token(auth))
    type_id = (await client.post(URL, json=body(), headers=headers)).json()["id"]
    window_id = (
        await client.post(f"{URL}/{type_id}/windows", json=window(), headers=headers)
    ).json()["id"]
    return auth, type_id, window_id


async def test_an_offering_with_a_live_window_uses_its_own(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth, type_id, _ = await offering_with_window(api_client, db_engine, "own-win-live")

    listed = await own(api_client, auth, type_id)
    restored = (
        await api_client.post(f"{URL}/{type_id}/restore", headers=bearer(api_token(auth)))
    ).json()

    assert listed["uses_own_windows"] is True
    assert restored["uses_own_windows"] is True


async def test_an_offering_with_no_window_follows_the_calendar(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth = await as_mentor(db_engine, "own-win-none")
    created = (await api_client.post(URL, json=body(), headers=bearer(api_token(auth)))).json()

    assert (await own(api_client, auth, created["id"]))["uses_own_windows"] is False


async def test_a_deleted_window_no_longer_counts(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth, type_id, window_id = await offering_with_window(api_client, db_engine, "own-win-del")

    deleted = await api_client.delete(
        f"{URL}/{type_id}/windows/{window_id}", headers=bearer(api_token(auth))
    )

    assert deleted.status_code == 200, deleted.text
    assert (await own(api_client, auth, type_id))["uses_own_windows"] is False


async def test_a_switched_off_window_no_longer_counts(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The same rule slots use: only an active, undeleted window replaces hours."""
    auth, type_id, window_id = await offering_with_window(api_client, db_engine, "own-win-off")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE session_type_scheduling_windows SET is_active = false WHERE id = :w"),
            {"w": window_id},
        )

    assert (await own(api_client, auth, type_id))["uses_own_windows"] is False
