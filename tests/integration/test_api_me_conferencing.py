"""A mentor reads and sets their default video provider (`/me/conferencing`).

The default is what every offering that chose nothing is held on, and what a
mentor who never chose gets from the platform: EduFurther video, `daily`
(owner decision 2026-10-01, replacing the `google_meet` fallback).
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_me_session_type_writes import as_mentee, as_mentor
from tests.integration.test_conferencing_options import add_option

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

URL = "/api/v1/me/conferencing"
ROOM = "https://rooms.example.org/ada"


async def defaults_of(engine: AsyncEngine, mentor: UUID) -> list[tuple[str, str | None]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT provider, custom_url FROM mentor_conferencing_options "
                "WHERE user_id = :u AND is_default"
            ),
            {"u": mentor},
        )
        return [(str(row[0]), row[1]) for row in rows]


async def test_a_mentor_who_never_chose_gets_edufurther_video(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth_id = await as_mentor(db_engine, "conf-none")

    response = await api_client.get(URL, headers=bearer(api_token(auth_id)))

    assert response.status_code == 200, response.text
    assert response.json() == {"provider": "daily", "custom_url": None, "is_default_choice": True}


async def test_a_mentor_sets_a_personal_link_and_reads_it_back(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth_id = await as_mentor(db_engine, "conf-custom")
    headers = bearer(api_token(auth_id))

    written = await api_client.patch(
        URL, json={"provider": "custom", "custom_url": ROOM}, headers=headers
    )
    read = await api_client.get(URL, headers=headers)

    assert written.status_code == 200, written.text
    assert read.json() == {"provider": "custom", "custom_url": ROOM, "is_default_choice": False}
    assert await defaults_of(db_engine, mentor) == [("custom", ROOM)]


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"provider": "custom", "custom_url": None}, id="custom-without-url"),
        pytest.param({"provider": "daily", "custom_url": ROOM}, id="daily-with-url"),
        pytest.param({"provider": "google_meet", "custom_url": ROOM}, id="meet-with-url"),
        pytest.param({"provider": "custom", "custom_url": "http://x.org/r"}, id="not-https"),
        pytest.param(
            {"provider": "custom", "custom_url": "https://ada:pw@x.org/r"}, id="credentials"
        ),
        pytest.param({"provider": "custom", "custom_url": "https:///r"}, id="no-host"),
        pytest.param({"provider": "zoom", "custom_url": None}, id="zoom"),
    ],
)
async def test_an_impossible_choice_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, body: dict[str, object]
) -> None:
    mentor, auth_id = await as_mentor(db_engine, f"conf-bad-{uuid4().hex[:8]}")

    response = await api_client.patch(URL, json=body, headers=bearer(api_token(auth_id)))

    assert response.status_code == 422, response.text
    assert await defaults_of(db_engine, mentor) == []


async def test_switching_leaves_exactly_one_default(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The partial unique index allows one default; the swap clears before it sets."""
    mentor, auth_id = await as_mentor(db_engine, "conf-swap")
    await add_option(db_engine, mentor, provider="google_meet", is_default=True)
    headers = bearer(api_token(auth_id))

    to_custom = await api_client.patch(
        URL, json={"provider": "custom", "custom_url": ROOM}, headers=headers
    )
    back_to_meet = await api_client.patch(
        URL, json={"provider": "google_meet", "custom_url": None}, headers=headers
    )

    assert to_custom.status_code == 200, to_custom.text
    assert back_to_meet.status_code == 200, back_to_meet.text
    assert await defaults_of(db_engine, mentor) == [("google_meet", None)]


async def test_a_new_personal_link_replaces_the_old_one(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth_id = await as_mentor(db_engine, "conf-relink")
    headers = bearer(api_token(auth_id))
    await api_client.patch(URL, json={"provider": "custom", "custom_url": ROOM}, headers=headers)

    await api_client.patch(
        URL, json={"provider": "custom", "custom_url": ROOM + "-2"}, headers=headers
    )

    assert await defaults_of(db_engine, mentor) == [("custom", ROOM + "-2")]


async def test_someone_who_is_not_a_mentor_gets_404(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = await as_mentee(db_engine, "conf-mentee")
    headers = bearer(api_token(auth_id))

    read = await api_client.get(URL, headers=headers)
    written = await api_client.patch(
        URL, json={"provider": "daily", "custom_url": None}, headers=headers
    )

    assert read.status_code == 404, read.text
    assert written.status_code == 404, written.text


async def test_without_a_token_it_is_401(api_client: httpx.AsyncClient) -> None:
    assert (await api_client.get(URL)).status_code == 401
    assert (await api_client.patch(URL, json={"provider": "daily"})).status_code == 401


async def test_the_routes_name_no_user_but_the_caller() -> None:
    """`/me` only: no path parameter can point it at another mentor's options."""
    from app.api.routes.me_conferencing import router

    assert {route.path for route in router.routes} == {URL}  # type: ignore[attr-defined]
