"""A session type's icon, and the "interviewing" stage (Session Types #18, #10).

`icon` is one of nine Material Symbols names, in the design's order, or null for
the client's automatic pick. `interviewing` joins `ApplicationStage` between
`revisions` and `other`, the design's order; one stage per type still.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import add_availability
from tests.integration.test_api_me_session_type_writes import URL, as_mentor, body

from app.domain.enums import ApplicationStage, SessionTypeIcon
from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]

ICONS = [
    "video_call", "edit_document", "find_in_page", "school", "payments",
    "record_voice_over", "quiz", "badge", "lightbulb",
]  # fmt: skip


def test_the_icons_are_the_designs_in_its_order() -> None:
    assert [i.value for i in SessionTypeIcon] == ICONS


def test_interviewing_sits_before_other() -> None:
    stages = [s.value for s in ApplicationStage]
    assert stages.index("interviewing") == stages.index("other") - 1
    assert stages.index("interviewing") == stages.index("revisions") + 1


async def test_an_icon_is_written_and_read_back_everywhere(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "icon-write")
    headers = bearer(api_token(auth))

    created = await api_client.post(URL, json=body(icon="quiz"), headers=headers)
    await add_availability(db_engine, mentor)
    own = (await api_client.get(URL, headers=headers)).json()["data"]
    public = (await api_client.get(f"/api/v1/users/{mentor}/session-types")).json()["data"]

    assert created.status_code == 201
    assert own[0]["icon"] == "quiz"
    assert public[0]["icon"] == "quiz"


async def test_no_icon_is_automatic(api_client: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    _, auth = await as_mentor(db_engine, "icon-none")
    headers = bearer(api_token(auth))

    await api_client.post(URL, json=body(), headers=headers)
    (row,) = (await api_client.get(URL, headers=headers)).json()["data"]

    assert row["icon"] is None


async def test_patch_sets_clears_and_leaves_the_icon(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth = await as_mentor(db_engine, "icon-patch")
    headers = bearer(api_token(auth))
    type_id = (await api_client.post(URL, json=body(), headers=headers)).json()["id"]

    async def icon() -> object:
        return (await api_client.get(URL, headers=headers)).json()["data"][0]["icon"]

    await api_client.patch(f"{URL}/{type_id}", json={"icon": "badge"}, headers=headers)
    assert await icon() == "badge"
    await api_client.patch(f"{URL}/{type_id}", json={"name": "Renamed"}, headers=headers)
    assert await icon() == "badge"
    await api_client.patch(f"{URL}/{type_id}", json={"icon": None}, headers=headers)
    assert await icon() is None


async def test_an_unknown_icon_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth = await as_mentor(db_engine, "icon-bad")

    response = await api_client.post(URL, json=body(icon="rocket"), headers=bearer(api_token(auth)))

    assert response.status_code == 422


async def test_the_database_refuses_an_unknown_icon(db_engine: AsyncEngine) -> None:
    from sqlalchemy.exc import IntegrityError

    mentor, _ = await as_mentor(db_engine, "icon-ck")
    with pytest.raises(IntegrityError):
        async with db_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO session_types (mentor_user_id, name, icon) "
                    "VALUES (:u, 'x', 'rocket')"
                ),
                {"u": mentor},
            )


async def test_interviewing_is_a_stage_a_type_can_have(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth = await as_mentor(db_engine, "stage-interviewing")
    headers = bearer(api_token(auth))

    created = await api_client.post(
        URL, json=body(application_stage="interviewing"), headers=headers
    )
    (row,) = (await api_client.get(URL, headers=headers)).json()["data"]

    assert created.status_code == 201
    assert row["application_stage"] == "interviewing"
