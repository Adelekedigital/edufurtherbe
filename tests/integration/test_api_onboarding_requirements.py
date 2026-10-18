"""A mentee finishes onboarding with four answers, and `/me` says which are owed.

Mentee Onboarding v5 B3 and H5 (decision 261): at least one live help area, an
origin country, a degree goal and a field of study. The `409` names what is
missing in step order, and `/me` reports the same list by the same rule.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_onboarding_completion import PATH, balance_of, seed

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]

#: The statement that takes each answer back away from a seeded mentee.
UNDO = {
    "help": "DELETE FROM mentee_goal_needs WHERE user_id = :u",
    "origin": "UPDATE user_profiles SET origin_country_id = NULL WHERE user_id = :u",
    "degree": "UPDATE mentee_goals SET degree_goal_id = NULL WHERE user_id = :u",
    "field": "UPDATE mentee_goals SET field_of_study = NULL WHERE user_id = :u",
}


async def run(engine: AsyncEngine, sql: str, user_id: UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(sql), {"u": user_id})


async def onboarding_on_me(client: httpx.AsyncClient, auth_id: UUID) -> object:
    """The two facts these tests are about; `last_step` has its own file."""
    me = (await client.get("/api/v1/me", headers=bearer(api_token(auth_id)))).json()
    return {key: me["onboarding"][key] for key in ("complete", "missing")}


@pytest.mark.parametrize("code", list(UNDO))
async def test_each_missing_answer_is_refused_and_named(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient, code: str
) -> None:
    auth_id = uuid4()
    user_id = await seed(db_engine, auth_id)
    await run(db_engine, UNDO[code], user_id)

    response = await api_client.post(PATH, headers=bearer(api_token(auth_id)))

    assert response.status_code == 409
    assert response.json()["type"] == "/problems/onboarding-incomplete"
    assert response.json()["missing"] == [code]
    assert await balance_of(db_engine, user_id) == 0


async def test_every_missing_answer_comes_in_step_order(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    auth_id = uuid4()
    await seed(db_engine, auth_id, answers=False)

    response = await api_client.post(PATH, headers=bearer(api_token(auth_id)))

    assert response.json()["missing"] == ["help", "origin", "degree", "field"]


async def test_a_retired_help_area_does_not_count(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """The need must name an offering the platform still offers, the rule the
    goal ranking already uses (`_live_goals`)."""
    auth_id = uuid4()
    user_id = await seed(db_engine, auth_id)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE service_offerings SET is_active = false WHERE id IN "
                "(SELECT service_offering_id FROM mentee_goal_needs WHERE user_id = :u)"
            ),
            {"u": user_id},
        )
    try:
        response = await api_client.post(PATH, headers=bearer(api_token(auth_id)))
    finally:
        await run(
            db_engine,
            "UPDATE service_offerings SET is_active = true WHERE id IN "
            "(SELECT service_offering_id FROM mentee_goal_needs WHERE user_id = :u)",
            user_id,
        )

    assert response.status_code == 409
    assert response.json()["missing"] == ["help"]


async def test_a_mentee_with_every_answer_is_granted_the_credit(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    auth_id = uuid4()
    user_id = await seed(db_engine, auth_id)

    response = await api_client.post(PATH, headers=bearer(api_token(auth_id)))

    assert response.status_code == 201
    assert await balance_of(db_engine, user_id) == 1


async def test_a_mentor_finishes_without_the_mentee_answers(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Mentor completion is unchanged (PRD H7)."""
    auth_id = uuid4()
    user_id = await seed(db_engine, auth_id, goal=False, mentor=True, answers=False)

    response = await api_client.post(PATH, headers=bearer(api_token(auth_id)))

    assert response.status_code == 201
    assert await balance_of(db_engine, user_id) == 1


async def test_me_names_what_a_new_account_owes(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    auth_id = uuid4()
    await seed(db_engine, auth_id, profile=False, goal=False)

    assert await onboarding_on_me(api_client, auth_id) == {
        "complete": False,
        "missing": ["help", "origin", "degree", "field"],
    }


@pytest.mark.parametrize("code", list(UNDO))
async def test_me_and_the_refusal_agree(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient, code: str
) -> None:
    auth_id = uuid4()
    user_id = await seed(db_engine, auth_id)
    await run(db_engine, UNDO[code], user_id)

    refusal = await api_client.post(PATH, headers=bearer(api_token(auth_id)))

    assert await onboarding_on_me(api_client, auth_id) == {
        "complete": False,
        "missing": refusal.json()["missing"],
    }


async def test_me_says_complete_once_finished(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    auth_id = uuid4()
    await seed(db_engine, auth_id)
    await api_client.post(PATH, headers=bearer(api_token(auth_id)))

    assert await onboarding_on_me(api_client, auth_id) == {"complete": True, "missing": []}


async def test_a_user_finished_under_the_old_rule_stays_complete(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """`complete` and `missing` are separate facts: a migrated user whose
    `completed_at` the ETL wrote is complete, and `missing` still names what
    the current rule would ask, so the client can send them to finish it."""
    auth_id = uuid4()
    user_id = await seed(db_engine, auth_id, answers=False)
    await run(
        db_engine,
        "INSERT INTO user_onboarding (user_id, completed_at) VALUES (:u, now())",
        user_id,
    )

    assert await onboarding_on_me(api_client, auth_id) == {
        "complete": True,
        "missing": ["help", "origin", "degree", "field"],
    }


async def test_a_saved_step_is_not_complete(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """A row with a step and no `completed_at` is started, not finished."""
    auth_id = uuid4()
    await seed(db_engine, auth_id)
    await api_client.put(
        "/api/v1/me/onboarding", json={"last_step": "plans"}, headers=bearer(api_token(auth_id))
    )

    assert await onboarding_on_me(api_client, auth_id) == {"complete": False, "missing": []}


async def test_a_retry_stays_200_after_an_answer_is_cleared(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**Idempotent, as published.** A finished mentee who later clears an
    answer is still finished: a retry answers `200` with the original date and
    grants nothing, rather than a `409` for an account that is done."""
    auth_id = uuid4()
    user_id = await seed(db_engine, auth_id)
    headers = bearer(api_token(auth_id))
    first = await api_client.post(PATH, headers=headers)
    await run(db_engine, UNDO["field"], user_id)

    again = await api_client.post(PATH, headers=headers)

    assert again.status_code == 200
    assert again.json()["completed_at"] == first.json()["completed_at"]
    assert await balance_of(db_engine, user_id) == 1


async def test_a_migrated_user_holding_the_starter_retries_to_200(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """The ETL writes `completed_at` and the starter lot, and no migrated goal
    has a field of study, so the current bar must not refuse their retry."""
    auth_id = uuid4()
    user_id = await seed(db_engine, auth_id, answers=False)
    await run(
        db_engine,
        "INSERT INTO user_onboarding (user_id, completed_at) VALUES (:u, now())",
        user_id,
    )
    await run(
        db_engine,
        "INSERT INTO credit_lots (user_id, source, quantity_granted, quantity_remaining) "
        "VALUES (:u, 'profile_completed', 1, 1)",
        user_id,
    )

    response = await api_client.post(PATH, headers=bearer(api_token(auth_id)))

    assert response.status_code == 200
    assert await balance_of(db_engine, user_id) == 1


async def test_a_finished_user_without_the_starter_still_meets_the_bar(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """The anti-farming half: only a user already paid skips the check. One the
    ETL marked finished with no credit earns it by the current rule."""
    auth_id = uuid4()
    user_id = await seed(db_engine, auth_id, answers=False)
    await run(
        db_engine,
        "INSERT INTO user_onboarding (user_id, completed_at) VALUES (:u, now())",
        user_id,
    )

    response = await api_client.post(PATH, headers=bearer(api_token(auth_id)))

    assert response.status_code == 409
    assert await balance_of(db_engine, user_id) == 0


async def test_a_mentor_owes_no_mentee_answers(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """`missing` is what completion would still ask; a mentor's bar has no
    mentee answers, so a mentor is never sent to the mentee steps."""
    auth_id = uuid4()
    await seed(db_engine, auth_id, goal=False, mentor=True, answers=False)

    before = await onboarding_on_me(api_client, auth_id)
    await api_client.post(PATH, headers=bearer(api_token(auth_id)))
    after = await onboarding_on_me(api_client, auth_id)

    assert before == {"complete": False, "missing": []}
    assert after == {"complete": True, "missing": []}
