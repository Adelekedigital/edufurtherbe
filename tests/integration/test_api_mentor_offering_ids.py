"""A mentor's `offering_ids`: an unknown, retired or repeated id is a `422`, not a 500.

Before this, the list went straight to `mentor_service_offerings`: an id naming
no offering failed the foreign key and a repeat failed the unique pair, both as
a 500. Now both are refused at a pointer before anything is written, on the
create and the edit alike, through the goal's own checks (`refuse_repeats`,
`_refuse_unknown_offering_ids`).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import make_public_mentor, token_for
from tests.integration.test_api_writes import make_user, url

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def _offerings(engine: AsyncEngine, count: int) -> list[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT id FROM service_offerings WHERE is_active ORDER BY id LIMIT :n"),
            {"n": count},
        )
    return [str(r.id) for r in rows]


async def _held(engine: AsyncEngine, mentor: UUID) -> list[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT service_offering_id FROM mentor_service_offerings "
                "WHERE mentor_user_id = :u ORDER BY service_offering_id"
            ),
            {"u": mentor},
        )
    return [str(r[0]) for r in rows]


def _pointers(response: httpx.Response) -> list[str]:
    return [e["pointer"] for e in response.json()["errors"]]


async def _patch(
    client: httpx.AsyncClient, engine: AsyncEngine, mentor: UUID, body: dict[str, Any]
) -> httpx.Response:
    headers = bearer(await token_for(engine, mentor))
    return await client.patch(url(mentor, "mentor-profile"), headers=headers, json=body)


async def test_known_offerings_save(api_client: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    mentor = await make_public_mentor(db_engine, "off-ok")
    a, b = await _offerings(db_engine, 2)

    response = await _patch(api_client, db_engine, mentor, {"offering_ids": [a, b]})

    assert response.status_code == 200, response.text
    assert await _held(db_engine, mentor) == sorted([a, b])


async def test_an_unknown_offering_is_a_422_at_its_index_and_writes_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "off-unknown")
    (a,) = await _offerings(db_engine, 1)
    before = await _held(db_engine, mentor)

    response = await _patch(
        api_client, db_engine, mentor, {"offering_ids": [a, str(uuid4())], "headline": "No"}
    )

    assert response.status_code == 422, response.text
    assert _pointers(response) == ["/offering_ids/1"]
    assert await _held(db_engine, mentor) == before


async def test_a_retired_offering_is_refused_like_an_unknown_one(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "off-retired")
    a, b = await _offerings(db_engine, 2)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE service_offerings SET is_active = false WHERE id = :o"), {"o": b}
        )
    try:
        response = await _patch(api_client, db_engine, mentor, {"offering_ids": [a, b]})
    finally:
        async with db_engine.begin() as conn:
            await conn.execute(
                text("UPDATE service_offerings SET is_active = true WHERE id = :o"), {"o": b}
            )

    assert response.status_code == 422, response.text
    assert _pointers(response) == ["/offering_ids/1"]


async def test_an_offering_twice_is_a_422_at_the_list(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "off-twice")
    (a,) = await _offerings(db_engine, 1)

    response = await _patch(api_client, db_engine, mentor, {"offering_ids": [a, a]})

    assert response.status_code == 422, response.text
    assert _pointers(response) == ["/offering_ids"]


async def test_applying_with_an_unknown_offering_creates_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, f"off-apply-{uuid4().hex[:6]}@example.com")

    response = await api_client.post(
        url(user_id, "mentor-profile"),
        headers=bearer(api_token(auth_id)),
        json={"headline": "Hi", "offering_ids": [str(uuid4())]},
    )

    assert response.status_code == 422, response.text
    assert _pointers(response) == ["/offering_ids/0"]
    async with db_engine.connect() as conn:
        made = await conn.execute(
            text("SELECT count(*) FROM mentor_profiles WHERE user_id = :u"), {"u": user_id}
        )
    assert made.scalar_one() == 0
