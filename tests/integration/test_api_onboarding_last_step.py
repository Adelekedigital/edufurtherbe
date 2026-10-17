"""``PUT /api/v1/me/onboarding`` — where the caller got to (decision 260).

The guards that matter: the step is one of ``domain.onboarding.STEPS``, the
write never touches ``completed_at``, and it lands on the caller's row only.
"""

import asyncio
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.integration.factories import until_blocked

from app.infra.db.onboarding_writer import record_last_step
from conftest import add_user, api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

PATH = "/api/v1/me/onboarding"


async def seed(engine: AsyncEngine, auth_id: UUID) -> UUID:
    async with engine.begin() as conn:
        user_id = await add_user(
            conn,
            f"{auth_id}@example.com",
            auth_id=auth_id,
            primary_role="mentee",
            timezone="Africa/Lagos",
        )
        return UUID(str(user_id))


async def stored(engine: AsyncEngine, user_id: UUID) -> Any:
    async with engine.begin() as conn:
        return (
            await conn.execute(
                text("SELECT last_step, completed_at FROM user_onboarding WHERE user_id = :u"),
                {"u": user_id},
            )
        ).one_or_none()


async def put(client: httpx.AsyncClient, auth_id: UUID, body: Any) -> httpx.Response:
    return await client.put(PATH, json=body, headers=bearer(api_token(auth_id)))


async def test_a_first_step_creates_the_row(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    auth_id = uuid4()
    user_id = await seed(db_engine, auth_id)
    assert await stored(db_engine, user_id) is None

    response = await put(api_client, auth_id, {"last_step": "help"})

    assert response.status_code == 200
    assert response.json() == {"last_step": "help", "completed_at": None}
    row = await stored(db_engine, user_id)
    assert row.last_step == "help"
    assert row.completed_at is None


async def test_both_steps_are_recorded_in_turn(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    auth_id = uuid4()
    user_id = await seed(db_engine, auth_id)

    await put(api_client, auth_id, {"last_step": "help"})
    response = await put(api_client, auth_id, {"last_step": "plans"})

    assert response.status_code == 200
    assert response.json()["last_step"] == "plans"
    assert (await stored(db_engine, user_id)).last_step == "plans"


async def test_moving_back_records_the_value_sent(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    auth_id = uuid4()
    user_id = await seed(db_engine, auth_id)

    await put(api_client, auth_id, {"last_step": "plans"})
    response = await put(api_client, auth_id, {"last_step": "help"})

    assert response.status_code == 200
    assert response.json()["last_step"] == "help"
    assert (await stored(db_engine, user_id)).last_step == "help"


async def test_a_finished_onboarding_keeps_its_completed_at(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Saving a step neither clears nor moves the date somebody finished."""
    auth_id = uuid4()
    user_id = await seed(db_engine, auth_id)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_onboarding (user_id, last_step, completed_at) "
                "VALUES (:u, 'plans', '2026-01-02T03:04:05Z')"
            ),
            {"u": user_id},
        )
    before = (await stored(db_engine, user_id)).completed_at

    response = await put(api_client, auth_id, {"last_step": "help"})

    assert response.status_code == 200
    body = response.json()
    assert body["last_step"] == "help"
    assert body["completed_at"] is not None
    row = await stored(db_engine, user_id)
    assert row.completed_at == before
    assert row.last_step == "help"


async def test_get_returns_the_saved_step(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    auth_id = uuid4()
    await seed(db_engine, auth_id)
    headers = bearer(api_token(auth_id))
    assert (await api_client.get(PATH, headers=headers)).status_code == 404

    await put(api_client, auth_id, {"last_step": "plans"})
    response = await api_client.get(PATH, headers=headers)

    assert response.status_code == 200
    assert response.json() == {"last_step": "plans", "completed_at": None}


async def test_only_the_callers_row_is_written(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    caller_auth, other_auth = uuid4(), uuid4()
    caller = await seed(db_engine, caller_auth)
    other = await seed(db_engine, other_auth)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO user_onboarding (user_id, last_step) VALUES (:u, 'help')"),
            {"u": other},
        )

    response = await put(api_client, caller_auth, {"last_step": "plans"})

    assert response.status_code == 200
    assert (await stored(db_engine, caller)).last_step == "plans"
    assert (await stored(db_engine, other)).last_step == "help"


@pytest.mark.parametrize(
    "body",
    [
        {"last_step": "finished"},
        {"last_step": "HELP"},
        {"last_step": ""},
        {"last_step": None},
        {"last_step": 1},
        {},
    ],
    ids=["unknown", "wrong-case", "empty", "null", "number", "missing"],
)
async def test_anything_but_a_known_step_is_a_422_at_last_step(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient, body: Any
) -> None:
    auth_id = uuid4()
    user_id = await seed(db_engine, auth_id)

    response = await put(api_client, auth_id, body)

    assert response.status_code == 422
    assert [e["pointer"] for e in response.json()["errors"]] == ["/last_step"]
    assert await stored(db_engine, user_id) is None


async def test_an_extra_field_is_a_422(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """`completed_at` above all: a client must not be able to finish onboarding
    by saying so, which is the completion endpoint's whole job."""
    auth_id = uuid4()
    user_id = await seed(db_engine, auth_id)

    response = await put(
        api_client, auth_id, {"last_step": "help", "completed_at": "2026-01-01T00:00:00Z"}
    )

    assert response.status_code == 422
    assert [e["pointer"] for e in response.json()["errors"]] == ["/completed_at"]
    assert await stored(db_engine, user_id) is None


async def test_no_token_is_a_401(api_client: httpx.AsyncClient) -> None:
    response = await api_client.put(PATH, json={"last_step": "help"})

    assert response.status_code == 401


async def test_two_first_saves_racing_both_land(db_engine: AsyncEngine) -> None:
    """The second insert waits on the first's unique `user_id` and then updates
    it. A read-then-insert would see no row in both and fail the second with a
    unique violation, a 500 on a refresh."""
    user_id = await seed(db_engine, uuid4())

    async with AsyncSession(db_engine) as first, AsyncSession(db_engine) as second:
        await record_last_step(first, user_id, "help")
        racing = asyncio.create_task(record_last_step(second, user_id, "plans"))
        await until_blocked(db_engine)
        await first.commit()
        row = await racing
        await second.commit()

    assert row.last_step == "plans"
    assert (await stored(db_engine, user_id)).last_step == "plans"
