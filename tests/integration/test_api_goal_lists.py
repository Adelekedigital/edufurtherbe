"""The goal's `country_ids` and `need_ids`, refused at the boundary.

Each list is checked before anything is written. A repeated id is a `422` at
the list and an id that names no catalogue row is a `422` at its index. Without
the check, the first meets a unique pair index and the second a foreign key,
each as a 500. Replacements arriving together each land whole.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_writes import make_user, url

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def _owner(engine: AsyncEngine) -> tuple[UUID, dict[str, str]]:
    auth_id = uuid4()
    user = await make_user(engine, auth_id, f"goal-{auth_id.hex[:8]}@example.com")
    return user, bearer(api_token(auth_id))


async def _ids(engine: AsyncEngine, table: str, count: int) -> list[str]:
    """`count` catalogue ids; offerings only live ones, as the catalogue lists."""
    live = " WHERE is_active" if table == "service_offerings" else ""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(f"SELECT id FROM {table}{live} ORDER BY id LIMIT :n"),  # noqa: S608 - fixed names
            {"n": count},
        )
        return [str(r) for r in rows.scalars()]


async def _put(
    client: httpx.AsyncClient, user: UUID, headers: dict[str, str], body: dict[str, Any]
) -> httpx.Response:
    return await client.put(url(user, "goal"), headers=headers, json=body)


async def _goal(client: httpx.AsyncClient, user: UUID, headers: dict[str, str]) -> dict[str, Any]:
    response = await client.get(url(user, "goal"), headers=headers)
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


async def _stored_countries(engine: AsyncEngine, user: UUID) -> list[str]:
    """The goal's countries by id, in priority order: the read gives code and name."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT country_id FROM mentee_goal_countries WHERE user_id = :u ORDER BY priority"
            ),
            {"u": user},
        )
        return [str(r) for r in rows.scalars()]


def _pointers(response: httpx.Response) -> list[str]:
    return [e["pointer"] for e in response.json()["errors"]]


async def test_known_lists_are_saved(api_client: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    user, headers = await _owner(db_engine)
    countries = await _ids(db_engine, "countries", 2)
    needs = await _ids(db_engine, "service_offerings", 2)

    saved = await _put(
        api_client, user, headers, {"notes": "A", "country_ids": countries, "need_ids": needs}
    )

    assert saved.status_code == 200, saved.text
    goal = await _goal(api_client, user, headers)
    assert await _stored_countries(db_engine, user) == countries
    assert sorted(n["id"] for n in goal["needs"]) == sorted(needs)


async def test_an_unknown_country_is_a_422_at_its_index_and_writes_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user, headers = await _owner(db_engine)
    a, b = await _ids(db_engine, "countries", 2)
    await _put(api_client, user, headers, {"notes": "A", "country_ids": [a]})

    refused = await _put(
        api_client, user, headers, {"notes": "Not saved", "country_ids": [b, str(uuid4())]}
    )

    assert refused.status_code == 422, refused.text
    assert _pointers(refused) == ["/country_ids/1"]
    goal = await _goal(api_client, user, headers)
    assert goal["notes"] == "A"
    assert await _stored_countries(db_engine, user) == [a]


async def test_an_unknown_need_is_a_422_at_its_index_and_writes_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user, headers = await _owner(db_engine)
    (need,) = await _ids(db_engine, "service_offerings", 1)
    await _put(api_client, user, headers, {"notes": "A", "need_ids": [need]})

    refused = await _put(
        api_client, user, headers, {"notes": "Not saved", "need_ids": [str(uuid4()), need]}
    )

    assert refused.status_code == 422, refused.text
    assert _pointers(refused) == ["/need_ids/0"]
    goal = await _goal(api_client, user, headers)
    assert goal["notes"] == "A"
    assert [n["id"] for n in goal["needs"]] == [need]


async def test_a_first_goal_with_an_unknown_id_creates_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user, headers = await _owner(db_engine)
    (country,) = await _ids(db_engine, "countries", 1)

    refused = await _put(
        api_client,
        user,
        headers,
        {"notes": "A", "country_ids": [country, str(uuid4())], "need_ids": [str(uuid4())]},
    )

    assert refused.status_code == 422, refused.text
    assert sorted(_pointers(refused)) == ["/country_ids/1", "/need_ids/0"]
    assert (await api_client.get(url(user, "goal"), headers=headers)).status_code == 404


@pytest.mark.parametrize("field", ["country_ids", "need_ids"])
async def test_a_repeated_id_is_a_422_at_the_list(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, field: str
) -> None:
    user, headers = await _owner(db_engine)
    table = "countries" if field == "country_ids" else "service_offerings"
    (value,) = await _ids(db_engine, table, 1)

    refused = await _put(api_client, user, headers, {"notes": "A", field: [value, value]})

    assert refused.status_code == 422, refused.text
    assert _pointers(refused) == [f"/{field}"]
    assert (await api_client.get(url(user, "goal"), headers=headers)).status_code == 404


async def test_a_retired_need_is_a_422_at_its_index(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The catalogue no longer lists a retired offering, so a write refuses it,
    as a session type's offerings do."""
    user, headers = await _owner(db_engine)
    live, retired = await _ids(db_engine, "service_offerings", 2)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE service_offerings SET is_active = false WHERE id = :i"), {"i": retired}
        )

    refused = await _put(api_client, user, headers, {"need_ids": [live, retired]})

    assert refused.status_code == 422, refused.text
    assert _pointers(refused) == ["/need_ids/1"]
    saved = await _put(api_client, user, headers, {"need_ids": [live]})
    assert saved.status_code == 200, saved.text


async def test_first_goals_arriving_together_make_one_whole_goal(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """One request creates the goal; the others lock it and replace it, as a
    second `PUT` would. Without that each loser meets `user_id` as a 500."""
    user, headers = await _owner(db_engine)
    every = await _ids(db_engine, "countries", 4)
    bodies = [{"notes": f"N{i}", "country_ids": [every[i]]} for i in range(4)]

    answers = await asyncio.gather(*(_put(api_client, user, headers, b) for b in bodies))

    assert [a.status_code for a in answers] == [200] * len(bodies), [a.text for a in answers]
    async with db_engine.connect() as conn:
        goals = (
            await conn.execute(
                text("SELECT count(*) FROM mentee_goals WHERE user_id = :u"), {"u": user}
            )
        ).scalar_one()
    assert goals == 1
    notes = (await _goal(api_client, user, headers))["notes"]
    stored = await _stored_countries(db_engine, user)
    assert {"notes": notes, "country_ids": stored} in bodies


async def test_replacements_arriving_together_each_land_whole(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The goal row is locked before its lists are replaced; without it the
    later insert meets the earlier one's rows on the unique pair and answers 500."""
    user, headers = await _owner(db_engine)
    every = await _ids(db_engine, "countries", 6)
    await _put(api_client, user, headers, {"notes": "A"})
    lists = [every[i : i + 3] for i in range(4)]

    answers = await asyncio.gather(
        *(_put(api_client, user, headers, {"country_ids": ids}) for ids in lists)
    )

    assert [a.status_code for a in answers] == [200] * len(lists), [a.text for a in answers]
    assert await _stored_countries(db_engine, user) in lists
