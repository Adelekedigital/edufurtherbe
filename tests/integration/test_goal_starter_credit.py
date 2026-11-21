"""Completing the four answers pays the starter credit at once to someone already
onboarded (decision 358).

171 migrated mentees finished onboarding in the legacy app and hold the
migration unlock but have no goal, so their level is 0 and they cannot book;
saving their answers used to pay nothing until the 1st. What carries weight:
the save that completes the four answers (help area, origin, degree, field)
pays one credit at once and says so, whichever of the goal or the profile it
is; a partial goal pays nothing; a repeat pays nothing; a full member editing
answers is paid nothing; someone mid-onboarding is paid by completion, not by
their answers.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_me_credits import (
    credits_of,
    onboard,
    seed_mentee,
    unlock,
)

from conftest import api_token, bearer, give_mentee_answers

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


def goal_url(user_id: UUID) -> str:
    return f"/api/v1/users/{user_id}/goal"


def profile_url(user_id: UUID) -> str:
    return f"/api/v1/users/{user_id}/profile"


async def ids(engine: AsyncEngine) -> dict[str, str]:
    """A real country, degree level and live help area to answer with."""
    async with engine.connect() as conn:
        country = (
            await conn.execute(text("SELECT id FROM countries ORDER BY code LIMIT 1"))
        ).scalar_one()
        degree = (
            await conn.execute(text("SELECT id FROM degree_levels ORDER BY slug LIMIT 1"))
        ).scalar_one()
        need = (
            await conn.execute(
                text("SELECT id FROM service_offerings WHERE is_active ORDER BY slug LIMIT 1")
            )
        ).scalar_one()
    return {"country": str(country), "degree": str(degree), "need": str(need)}


async def set_goal(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    auth_id: UUID,
    user_id: UUID,
    *,
    full: bool = True,
) -> None:
    """The goal's answers: degree, field and a help area (or a goal with none)."""
    known = await ids(engine)
    body = (
        {
            "degree_goal_id": known["degree"],
            "field_of_study": "Public Health",
            "need_ids": [known["need"]],
        }
        if full
        else {"notes": "Masters in the UK"}
    )
    response = await client.put(goal_url(user_id), json=body, headers=bearer(api_token(auth_id)))
    assert response.status_code in (200, 201), response.text


async def set_origin(
    client: httpx.AsyncClient, engine: AsyncEngine, auth_id: UUID, user_id: UUID
) -> None:
    known = await ids(engine)
    response = await client.patch(
        profile_url(user_id),
        json={"origin_country_id": known["country"]},
        headers=bearer(api_token(auth_id)),
    )
    assert response.status_code in (200, 201, 204), response.text


async def level_lots(engine: AsyncEngine, user_id: UUID) -> list[tuple[str, int]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT source::text, quantity_granted FROM credit_lots "
                "WHERE user_id = :u ORDER BY created_at"
            ),
            {"u": user_id},
        )
        return [(str(r[0]), int(r[1])) for r in rows]


async def granted_notices(engine: AsyncEngine, user_id: UUID) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM outbox_events "
                        "WHERE event_type = 'credits_granted' AND entity_id = :u"
                    ),
                    {"u": user_id},
                )
            ).scalar_one()
        )


async def a_group_c_mentee(engine: AsyncEngine, *, origin: bool = True) -> tuple[UUID, UUID]:
    """Group C: finished onboarding in the legacy app, unlocked, a profile
    (with their origin unless told otherwise), and no goal."""
    auth_id = uuid4()
    user_id = await seed_mentee(engine, auth_id, with_goal=False)
    await onboard(engine, user_id)
    await unlock(engine, user_id)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_profiles (user_id, origin_country_id) VALUES "
                "(:u, CASE WHEN :o THEN (SELECT id FROM countries ORDER BY code LIMIT 1) END)"
            ),
            {"u": user_id, "o": origin},
        )
    return auth_id, user_id


async def test_saving_the_last_answers_pays_one_credit_at_once_and_says_so(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id, user_id = await a_group_c_mentee(db_engine)
    assert (await credits_of(api_client, auth_id))["balance"] == 0

    await set_goal(api_client, db_engine, auth_id, user_id)

    assert await level_lots(db_engine, user_id) == [("level_top_up", 1)]
    assert (await credits_of(api_client, auth_id))["balance"] == 1
    assert await granted_notices(db_engine, user_id) == 1


async def test_a_goal_missing_an_answer_pays_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A goal row alone is not the four answers."""
    auth_id, user_id = await a_group_c_mentee(db_engine)

    await set_goal(api_client, db_engine, auth_id, user_id, full=False)

    assert await level_lots(db_engine, user_id) == []
    assert await granted_notices(db_engine, user_id) == 0


async def test_the_origin_saved_last_pays_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Origin lives on the profile, so the profile save can be the one that
    completes the answers."""
    auth_id, user_id = await a_group_c_mentee(db_engine, origin=False)
    await set_goal(api_client, db_engine, auth_id, user_id)
    assert await level_lots(db_engine, user_id) == []

    await set_origin(api_client, db_engine, auth_id, user_id)

    assert await level_lots(db_engine, user_id) == [("level_top_up", 1)]
    assert await granted_notices(db_engine, user_id) == 1


async def test_saving_again_pays_nothing_more(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id, user_id = await a_group_c_mentee(db_engine)
    await set_goal(api_client, db_engine, auth_id, user_id)

    await set_goal(api_client, db_engine, auth_id, user_id)
    await set_origin(api_client, db_engine, auth_id, user_id)

    assert await level_lots(db_engine, user_id) == [("level_top_up", 1)]
    assert await granted_notices(db_engine, user_id) == 1


async def test_a_full_member_editing_their_answers_is_paid_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Their answers were already complete before the save, so the before-check
    pays nothing, even with **no credit this month** (a member who spent their
    migrated balance to 0): only a save that completes the answers pays."""
    auth_id = uuid4()
    user_id = await seed_mentee(db_engine, auth_id, with_goal=True)
    await onboard(db_engine, user_id)
    await unlock(db_engine, user_id)
    async with db_engine.begin() as conn:
        await conn.execute(text("INSERT INTO user_profiles (user_id) VALUES (:u)"), {"u": user_id})
        await give_mentee_answers(conn, user_id)

    await set_goal(api_client, db_engine, auth_id, user_id)
    await set_origin(api_client, db_engine, auth_id, user_id)

    assert await level_lots(db_engine, user_id) == []
    assert await granted_notices(db_engine, user_id) == 0


async def test_a_mentee_still_onboarding_is_paid_by_completion_not_their_answers(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    user_id = await seed_mentee(db_engine, auth_id, with_goal=False)
    async with db_engine.begin() as conn:
        await conn.execute(text("INSERT INTO user_profiles (user_id) VALUES (:u)"), {"u": user_id})

    await set_origin(api_client, db_engine, auth_id, user_id)
    await set_goal(api_client, db_engine, auth_id, user_id)

    assert await level_lots(db_engine, user_id) == []
    assert await granted_notices(db_engine, user_id) == 0


async def test_an_unlocked_mentee_who_never_finished_onboarding_is_paid_by_completion(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The unlock alone reaches level 3 with a goal, so without the onboarded
    check their answers would pay them past the completion call."""
    auth_id = uuid4()
    user_id = await seed_mentee(db_engine, auth_id, with_goal=False)
    await unlock(db_engine, user_id)
    async with db_engine.begin() as conn:
        await conn.execute(text("INSERT INTO user_profiles (user_id) VALUES (:u)"), {"u": user_id})

    await set_origin(api_client, db_engine, auth_id, user_id)
    await set_goal(api_client, db_engine, auth_id, user_id)

    assert await level_lots(db_engine, user_id) == []
    assert await granted_notices(db_engine, user_id) == 0
