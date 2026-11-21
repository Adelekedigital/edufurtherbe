"""`PUT /me/seen-items/{key}` and `GET /me` → `seen_items` (decision 356).

What carries weight: a key is the caller's own and nobody else's, a repeat
keeps the first time it was seen, a bad key changes nothing, the store keeps
at most `MAX_SEEN_ITEMS` keys by dropping the oldest, and a preview cannot
write one.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import token_for

from app.domain.seen_items import KEY_MAX_LENGTH, MAX_SEEN_ITEMS
from conftest import add_user, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

ME = "/api/v1/me"
TOUR = "whats-new:2026-11-migration"


def seen(key: str) -> str:
    return f"/api/v1/me/seen-items/{key}"


async def signed_in(engine: AsyncEngine, **columns: Any) -> tuple[UUID, dict[str, str]]:
    async with engine.begin() as conn:
        user = await add_user(conn, f"{uuid4()}@example.test", **columns)
    return user, bearer(await token_for(engine, user))


async def seen_rows(engine: AsyncEngine, user: UUID) -> list[dict[str, Any]]:
    async with engine.begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT notice_key AS key, created_at FROM user_dismissed_notices "
                "WHERE user_id = :u ORDER BY created_at"
            ),
            {"u": user},
        )
        return [dict(row._mapping) for row in rows]


async def seen_items(api_client: httpx.AsyncClient, headers: dict[str, str]) -> list[str]:
    response = await api_client.get(ME, headers=headers)
    assert response.status_code == 200
    return response.json()["seen_items"]


async def test_nothing_seen_is_an_empty_list(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, headers = await signed_in(db_engine)

    assert await seen_items(api_client, headers) == []


async def test_a_seen_key_is_stored_and_shown_on_me(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user, headers = await signed_in(db_engine)

    response = await api_client.put(seen(TOUR), headers=headers)

    assert response.status_code == 204
    assert response.content == b""
    assert await seen_items(api_client, headers) == [TOUR]
    assert [row["key"] for row in await seen_rows(db_engine, user)] == [TOUR]


async def test_a_repeat_keeps_the_first_time_and_one_row(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user, headers = await signed_in(db_engine)
    assert (await api_client.put(seen(TOUR), headers=headers)).status_code == 204
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE user_dismissed_notices SET created_at = now() - interval '3 days' "
                "WHERE user_id = :u"
            ),
            {"u": user},
        )
    first = (await seen_rows(db_engine, user))[0]["created_at"]

    again = await api_client.put(seen(TOUR), headers=headers)

    assert again.status_code == 204
    rows = await seen_rows(db_engine, user)
    assert len(rows) == 1
    assert rows[0]["created_at"] == first


async def test_keys_come_back_oldest_first(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user, headers = await signed_in(db_engine)
    for key in ("a", "b", "c"):
        assert (await api_client.put(seen(key), headers=headers)).status_code == 204
    async with db_engine.begin() as conn:
        # Seen in the order c, a, b: the list follows created_at, not the key.
        for key, days in (("c", 3), ("a", 2), ("b", 1)):
            await conn.execute(
                text(
                    "UPDATE user_dismissed_notices "
                    "SET created_at = now() - make_interval(days => :d) "
                    "WHERE user_id = :u AND notice_key = :k"
                ),
                {"u": user, "k": key, "d": days},
            )

    assert await seen_items(api_client, headers) == ["c", "a", "b"]


@pytest.mark.parametrize(
    "key",
    [
        "Whats-new",  # upper case
        "-starts-with-a-dash",
        ":starts-with-a-colon",
        "has space",
        "has/slash",
        "émoji-or-accent",
        "k" * (KEY_MAX_LENGTH + 1),
    ],
)
async def test_a_bad_key_is_refused_and_changes_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, key: str
) -> None:
    user, headers = await signed_in(db_engine)

    response = await api_client.put(seen(key), headers=headers)

    assert response.status_code in (404, 422)
    assert await seen_rows(db_engine, user) == []


@pytest.mark.parametrize(
    "key", ["whats-new:2026-11-migration", "a", "0", "nudge.install_v2", "k" * KEY_MAX_LENGTH]
)
async def test_every_good_key_shape_is_accepted(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, key: str
) -> None:
    _, headers = await signed_in(db_engine)

    assert (await api_client.put(seen(key), headers=headers)).status_code == 204
    assert await seen_items(api_client, headers) == [key]


async def test_a_key_is_the_callers_own(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    alice, alice_headers = await signed_in(db_engine)
    _, bob_headers = await signed_in(db_engine)

    assert (await api_client.put(seen(TOUR), headers=alice_headers)).status_code == 204

    assert await seen_items(api_client, bob_headers) == []
    assert await seen_items(api_client, alice_headers) == [TOUR]
    assert [row["key"] for row in await seen_rows(db_engine, alice)] == [TOUR]


async def test_past_the_cap_the_oldest_is_dropped(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user, headers = await signed_in(db_engine)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_dismissed_notices (user_id, notice_key, created_at) "
                "SELECT :u, 'old-' || n, now() - make_interval(mins => :cap - n + 1) "
                "FROM generate_series(1, :cap) AS n"
            ),
            {"u": user, "cap": MAX_SEEN_ITEMS},
        )

    # A repeat of a stored key adds nothing and drops nothing.
    assert (await api_client.put(seen("old-1"), headers=headers)).status_code == 204
    assert len(await seen_rows(db_engine, user)) == MAX_SEEN_ITEMS

    assert (await api_client.put(seen("newest"), headers=headers)).status_code == 204

    keys = [row["key"] for row in await seen_rows(db_engine, user)]
    assert len(keys) == MAX_SEEN_ITEMS
    assert "old-1" not in keys
    assert keys[0] == "old-2"
    assert keys[-1] == "newest"


async def test_signed_out_is_refused(api_client: httpx.AsyncClient) -> None:
    response = await api_client.put(seen(TOUR))

    assert response.status_code == 401
