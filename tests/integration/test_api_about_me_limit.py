"""A bio is at most 1,500 characters (owner, 2026-10-17; decision 263).

The limit is on what is written: a longer bio already stored stays as it is,
and a save that does not send `about_me` is not refused because of it.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_profile_owner_edits import a_user, pointers
from tests.integration.test_api_writes import url

from app.domain.profile_text import ABOUT_ME_MAX_LENGTH

pytestmark = [pytest.mark.db, pytest.mark.anyio]


def test_the_limit_is_fifteen_hundred() -> None:
    assert ABOUT_ME_MAX_LENGTH == 1500


async def test_a_bio_at_the_limit_is_saved(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await a_user(db_engine, "about-ok")

    response = await api_client.patch(
        url(user_id, "profile"), json={"about_me": "y" * ABOUT_ME_MAX_LENGTH}, headers=headers
    )

    assert response.status_code == 204


async def test_a_bio_over_the_limit_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await a_user(db_engine, "about-long")

    response = await api_client.patch(
        url(user_id, "profile"), json={"about_me": "y" * (ABOUT_ME_MAX_LENGTH + 1)}, headers=headers
    )

    assert response.status_code == 422
    assert "/about_me" in pointers(response)


async def test_a_longer_stored_bio_does_not_block_other_edits(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await a_user(db_engine, "about-legacy")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO user_profiles (user_id, about_me) VALUES (:u, repeat('y', 4000))"),
            {"u": user_id},
        )

    response = await api_client.patch(
        url(user_id, "profile"), json={"gender": "female"}, headers=headers
    )

    assert response.status_code == 204


def test_the_spec_publishes_the_limit() -> None:
    from app.core.config import Settings
    from app.main import create_app

    spec = create_app(Settings(_env_file=None)).openapi()
    about = spec["components"]["schemas"]["UserProfileWrite"]["properties"]["about_me"]
    lengths = [part.get("maxLength") for part in about.get("anyOf", [about])]
    assert ABOUT_ME_MAX_LENGTH in lengths
