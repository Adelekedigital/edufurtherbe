"""Editing a degree from the owner's profile: what the form reads back and writes.

The profile editor prefills from `GET /users/{id}/education`, so the read must
carry what the write takes: `degree_level.id` for `degree_level_id`, and the
abbreviation the design picks ("MSc") in `degree_abbreviation` — never in the
legacy `degree_category`.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_writes import make_user, url

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def an_owner(engine: AsyncEngine, tag: str) -> tuple[UUID, dict[str, str]]:
    auth_id = uuid4()
    user_id = await make_user(engine, auth_id, f"edu-{tag}@example.com")
    return user_id, bearer(api_token(auth_id))


async def level_id(client: httpx.AsyncClient, slug: str) -> str:
    levels = (await client.get("/api/v1/catalog/degree-levels")).json()["data"]
    return next(str(level["id"]) for level in levels if level["code"] == slug)


async def only_entry(
    client: httpx.AsyncClient, user_id: UUID, headers: dict[str, str]
) -> dict[str, Any]:
    response = await client.get(url(user_id, "education"), headers=headers)
    assert response.status_code == 200, response.text
    (entry,) = response.json()["data"]
    return dict(entry)


async def test_the_abbreviation_is_saved_and_read_back_with_the_level_id(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await an_owner(db_engine, "save")
    masters = await level_id(api_client, "masters")

    created = await api_client.post(
        url(user_id, "education"),
        json={
            "school_name_raw": "University of Lagos",
            "degree_level_id": masters,
            "degree_abbreviation": "MSc",
        },
        headers=headers,
    )
    entry = await only_entry(api_client, user_id, headers)

    assert created.status_code == 201, created.text
    assert entry["degree_abbreviation"] == "MSc"
    assert entry["degree_level"]["id"] == masters
    assert entry["degree_level"]["code"] == "masters"
    assert entry["degree_category"] is None, "the label leaked into the legacy field"


@pytest.mark.parametrize("cleared", ["", None])
async def test_the_abbreviation_is_cleared_by_blank_or_null(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, cleared: str | None
) -> None:
    user_id, headers = await an_owner(db_engine, "clear-null" if cleared is None else "clear-blank")
    await api_client.post(
        url(user_id, "education"),
        json={"school_name_raw": "Makerere", "degree_abbreviation": "MBA"},
        headers=headers,
    )
    entry_id = (await only_entry(api_client, user_id, headers))["id"]

    response = await api_client.patch(
        url(user_id, f"education/{entry_id}"),
        json={"degree_abbreviation": cleared},
        headers=headers,
    )

    assert response.status_code in {200, 204}, response.text
    assert (await only_entry(api_client, user_id, headers))["degree_abbreviation"] is None


async def test_an_abbreviation_over_twenty_characters_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await an_owner(db_engine, "long")

    response = await api_client.post(
        url(user_id, "education"),
        json={"school_name_raw": "Legon", "degree_abbreviation": "x" * 21},
        headers=headers,
    )

    assert response.status_code == 422, response.text
    assert any(e["pointer"] == "/degree_abbreviation" for e in response.json()["errors"])


async def test_twenty_characters_is_accepted(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await an_owner(db_engine, "twenty")

    response = await api_client.post(
        url(user_id, "education"),
        json={"school_name_raw": "Legon", "degree_abbreviation": "x" * 20},
        headers=headers,
    )

    assert response.status_code == 201, response.text
