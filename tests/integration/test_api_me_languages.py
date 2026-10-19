"""The caller's own languages on `GET /me` (Mentee Settings, Settings, Profile).

`PUT /users/{id}/languages` writes them and, until this, only a mentor's public
profile read them back, so a mentee could save languages and never see them.
`languages` is the public profile's own read (`list_languages`, `LanguageRead`):
alphabetical, `{id, display_name, code}`, and `[]` when there are none.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_writes import make_user, url

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def _language(engine: AsyncEngine, code: str) -> tuple[UUID, str]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT id, display_name FROM languages WHERE code_639_3 = :c"), {"c": code}
            )
        ).one()
    return row.id, row.display_name


async def test_saved_languages_read_back_on_me_alphabetically(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    user = await make_user(db_engine, auth_id, "speaks@example.com")
    headers = bearer(api_token(auth_id))
    yor_id, yor_name = await _language(db_engine, "yor")
    eng_id, eng_name = await _language(db_engine, "eng")
    other = await make_user(db_engine, uuid4(), "other-speaker@example.com")
    fra_id, _ = await _language(db_engine, "fra")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO user_languages (user_id, language_id) VALUES (:u, :l)"),
            {"u": other, "l": fra_id},
        )

    saved = await api_client.put(
        url(user, "languages"),
        headers=headers,
        json={"languages": [{"language_id": str(yor_id)}, {"language_id": str(eng_id)}]},
    )
    assert saved.status_code == 204, saved.text

    body = (await api_client.get("/api/v1/me", headers=headers)).json()

    expected = sorted(
        [
            {"id": str(yor_id), "display_name": yor_name, "code": "yor"},
            {"id": str(eng_id), "display_name": eng_name, "code": "eng"},
        ],
        key=lambda language: language["display_name"],
    )
    assert body["languages"] == expected


async def test_no_languages_is_an_empty_list(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    await make_user(db_engine, auth_id, "silent@example.com")

    body = (await api_client.get("/api/v1/me", headers=bearer(api_token(auth_id)))).json()

    assert body["languages"] == []
