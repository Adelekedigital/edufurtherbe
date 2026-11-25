"""`GET /legacy-sessions/{legacy_id}`: where an old room link's session went.

Scoped to the session's two people in the query (`is_a_party`), like
`/sessions/{id}`, so a stranger, an admin and an unknown id all read the same
404 and the endpoint never says a session exists.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_sessions import make_session, make_user, pair

from conftest import api_token, bearer, legacy_id

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


def url(legacy_id: str) -> str:
    return f"/api/v1/legacy-sessions/{legacy_id}"


async def legacy_session(engine: AsyncEngine, tag: str) -> tuple[str, UUID, UUID, UUID]:
    """A migrated session: its legacy id, its id, and its two people's auth ids."""
    mentor, mentor_auth, mentee, mentee_auth = await pair(engine, tag)
    session_id = await make_session(engine, mentor, mentee, status="completed")
    legacy = legacy_id()
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET legacy_bubble_id = :l WHERE id = :s"),
            {"l": legacy, "s": session_id},
        )
    return legacy, session_id, mentor_auth, mentee_auth


@pytest.mark.parametrize("side", ["mentor", "mentee"])
async def test_either_party_finds_the_session(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, side: str
) -> None:
    legacy, session_id, mentor_auth, mentee_auth = await legacy_session(db_engine, f"legacy-{side}")
    auth = mentor_auth if side == "mentor" else mentee_auth

    response = await api_client.get(url(legacy), headers=bearer(api_token(auth)))

    assert response.status_code == 200
    assert response.json() == {"session_id": str(session_id)}


@pytest.mark.parametrize("admin", [False, True])
async def test_somebody_not_in_the_session_gets_the_unknown_id_404(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, admin: bool
) -> None:
    """The `WHERE` clause is the whole control: an admin is no party either."""
    tag = f"legacy-outsider-{'admin' if admin else 'member'}"
    legacy, _, _, _ = await legacy_session(db_engine, tag)
    outsider = uuid4()
    await make_user(db_engine, outsider, f"{tag}@example.test", admin=admin)
    headers = bearer(api_token(outsider))

    refused = await api_client.get(url(legacy), headers=headers)
    unknown = await api_client.get(url("1726837410805x205680694778626500"), headers=headers)

    assert refused.status_code == unknown.status_code == 404
    assert refused.json()["type"] == unknown.json()["type"]


async def test_a_signed_out_caller_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    legacy, _, _, _ = await legacy_session(db_engine, "legacy-signed-out")

    assert (await api_client.get(url(legacy))).status_code == 401


async def test_a_malformed_id_is_a_404_not_a_422(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    caller = uuid4()
    await make_user(db_engine, caller, "legacy-malformed@example.test")

    response = await api_client.get(url("not-an-id"), headers=bearer(api_token(caller)))

    assert response.status_code == 404
