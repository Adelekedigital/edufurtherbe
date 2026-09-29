"""A language list saved without its details keeps the ones already stored.

The profile editor only picks languages; it never sees proficiency or which one
is primary. The PUT replaces the whole list, so before this an editor save reset
every language to `fluent` and cleared the primary. Now an omitted field keeps
the stored value for a language already listed, and takes the default for a new
one; an explicit `is_primary: true` moves the primary to that language.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_writes import language_ids, make_user, url

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def stored(engine: AsyncEngine, user_id: UUID) -> dict[str, tuple[str, bool]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT l.display_name, ul.proficiency, ul.is_primary FROM user_languages ul "
                "JOIN languages l ON l.id = ul.language_id WHERE ul.user_id = :u"
            ),
            {"u": user_id},
        )
    return {row[0]: (str(row[1]), bool(row[2])) for row in rows}


async def a_speaker(
    client: httpx.AsyncClient, engine: AsyncEngine, tag: str
) -> tuple[UUID, dict[str, str], list[UUID]]:
    """A user who speaks English (native, primary) and Yoruba (basic)."""
    auth_id = uuid4()
    user_id = await make_user(engine, auth_id, f"keep-{tag}@example.com")
    headers = bearer(api_token(auth_id))
    ids = await language_ids(engine, "English", "Yoruba", "Hausa")
    response = await client.put(
        url(user_id, "languages"),
        json={
            "languages": [
                {"language_id": str(ids[0]), "proficiency": "native", "is_primary": True},
                {"language_id": str(ids[1]), "proficiency": "basic"},
            ]
        },
        headers=headers,
    )
    assert response.status_code == 204, response.text
    return user_id, headers, ids


def only_ids(*ids: UUID) -> dict[str, list[dict[str, str]]]:
    return {"languages": [{"language_id": str(i)} for i in ids]}


async def test_a_save_of_ids_only_keeps_proficiency_and_primary(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers, (english, yoruba, _) = await a_speaker(api_client, db_engine, "same")

    response = await api_client.put(
        url(user_id, "languages"), json=only_ids(english, yoruba), headers=headers
    )

    assert response.status_code == 204, response.text
    assert await stored(db_engine, user_id) == {
        "English": ("native", True),
        "Yoruba": ("basic", False),
    }


async def test_a_new_language_takes_the_defaults(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers, (english, yoruba, hausa) = await a_speaker(api_client, db_engine, "add")

    await api_client.put(
        url(user_id, "languages"), json=only_ids(english, yoruba, hausa), headers=headers
    )

    assert await stored(db_engine, user_id) == {
        "English": ("native", True),
        "Yoruba": ("basic", False),
        "Hausa": ("fluent", False),
    }


async def test_an_explicit_primary_moves_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers, (english, yoruba, _) = await a_speaker(api_client, db_engine, "move")

    response = await api_client.put(
        url(user_id, "languages"),
        json={
            "languages": [
                {"language_id": str(english)},
                {"language_id": str(yoruba), "is_primary": True},
            ]
        },
        headers=headers,
    )

    assert response.status_code == 204, response.text
    assert await stored(db_engine, user_id) == {
        "English": ("native", False),
        "Yoruba": ("basic", True),
    }


async def test_a_language_left_out_is_removed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers, (english, _, _) = await a_speaker(api_client, db_engine, "drop")

    await api_client.put(url(user_id, "languages"), json=only_ids(english), headers=headers)

    assert await stored(db_engine, user_id) == {"English": ("native", True)}


@pytest.mark.parametrize("field", ["proficiency", "is_primary"])
async def test_an_explicit_null_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, field: str
) -> None:
    """Omitted keeps; null is not a value either column can hold."""
    user_id, headers, (english, _, _) = await a_speaker(api_client, db_engine, f"null-{field}")

    response = await api_client.put(
        url(user_id, "languages"),
        json={"languages": [{"language_id": str(english), field: None}]},
        headers=headers,
    )

    assert response.status_code == 422, response.text
    assert (await stored(db_engine, user_id))["English"] == ("native", True)
