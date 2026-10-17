"""A mentee saves what they want to study on their goal (decision 259).

Onboarding asks "What do you want to study?" with suggestions or a typed
answer. It rides on `PUT /users/{id}/goal` and reads back on the goal and on
`/me`.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_writes import make_user, url

from app.domain.goals import FIELD_OF_STUDY_MAX_LENGTH, FIELD_OF_STUDY_SUGGESTIONS
from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def a_mentee(engine: AsyncEngine, tag: str) -> tuple[UUID, dict[str, str]]:
    auth_id = uuid4()
    user_id = await make_user(engine, auth_id, f"fos-{tag}-{uuid4().hex[:6]}@example.com")
    return user_id, bearer(api_token(auth_id))


async def stored(engine: AsyncEngine, user_id: UUID) -> object:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT field_of_study FROM mentee_goals WHERE user_id = :u"), {"u": user_id}
            )
        ).scalar_one()


async def test_a_field_of_study_is_saved_and_read_back(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await a_mentee(db_engine, "save")

    response = await api_client.put(
        url(user_id, "goal"), json={"field_of_study": "  Computer Science "}, headers=headers
    )
    goal = (await api_client.get(url(user_id, "goal"), headers=headers)).json()
    me = (await api_client.get("/api/v1/me", headers=headers)).json()

    assert response.status_code == 200
    assert await stored(db_engine, user_id) == "Computer Science"
    assert goal["field_of_study"] == "Computer Science"
    assert me["goal"]["field_of_study"] == "Computer Science"


async def test_an_omitted_field_of_study_is_left_alone(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await a_mentee(db_engine, "omit")
    await api_client.put(url(user_id, "goal"), json={"field_of_study": "Law"}, headers=headers)

    await api_client.put(url(user_id, "goal"), json={"notes": "Soon"}, headers=headers)

    assert await stored(db_engine, user_id) == "Law"


@pytest.mark.parametrize("value", [None, "", "   "])
async def test_null_or_blank_clears_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, value: object
) -> None:
    user_id, headers = await a_mentee(db_engine, "clear")
    await api_client.put(url(user_id, "goal"), json={"field_of_study": "Law"}, headers=headers)

    response = await api_client.put(
        url(user_id, "goal"), json={"field_of_study": value}, headers=headers
    )

    assert response.status_code == 200
    assert await stored(db_engine, user_id) is None


@pytest.mark.parametrize(
    "value",
    ["y" * 101, "Computer\nScience", "Law\u202e", "Law\u2028Studies", "---", "\u0301"],
    ids=["too-long", "newline", "rtl-override", "line-separator", "no-letter", "lone-mark"],
)
async def test_an_overlong_invisible_or_letterless_field_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, value: str
) -> None:
    user_id, headers = await a_mentee(db_engine, "bad")
    await api_client.put(url(user_id, "goal"), json={"field_of_study": "Law"}, headers=headers)

    response = await api_client.put(
        url(user_id, "goal"), json={"field_of_study": value}, headers=headers
    )

    assert response.status_code == 422
    assert "/field_of_study" in {e["pointer"] for e in response.json()["errors"]}
    assert await stored(db_engine, user_id) == "Law"


async def test_a_field_at_the_limit_is_kept_composed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Composed (NFC) like a name, and measured after composing."""
    user_id, headers = await a_mentee(db_engine, "limit")

    response = await api_client.put(
        url(user_id, "goal"), json={"field_of_study": "e\u0301" * 100}, headers=headers
    )

    assert response.status_code == 200
    assert await stored(db_engine, user_id) == "\u00e9" * 100


async def test_another_users_field_of_study_cannot_be_written(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    victim, victim_headers = await a_mentee(db_engine, "victim")
    await api_client.put(
        url(victim, "goal"), json={"field_of_study": "Law"}, headers=victim_headers
    )
    _, headers = await a_mentee(db_engine, "attacker")

    response = await api_client.put(
        url(victim, "goal"), json={"field_of_study": "Mallory"}, headers=headers
    )

    assert response.status_code == 404
    assert await stored(db_engine, victim) == "Law"


async def test_the_database_bound_is_the_api_bound(db_engine: AsyncEngine) -> None:
    """One limit, two layers (decision 259): the migration's literal must
    agree with the constant the API and the model read."""
    async with db_engine.connect() as conn:
        definition = (
            await conn.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'ck_mentee_goals_field_of_study_length'"
                )
            )
        ).scalar_one()

    assert f"<= {FIELD_OF_STUDY_MAX_LENGTH})" in definition


async def test_every_suggestion_saves_through_the_goal_write(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Each `/catalog/fields-of-study` suggestion (decision 262), saved as picked:
    the boundary and the column's `CHECK` both accept it, stored unchanged."""
    user_id, headers = await a_mentee(db_engine, "suggested")

    for name in FIELD_OF_STUDY_SUGGESTIONS:
        response = await api_client.put(
            url(user_id, "goal"), json={"field_of_study": name}, headers=headers
        )

        assert response.status_code == 200, name
        assert await stored(db_engine, user_id) == name
