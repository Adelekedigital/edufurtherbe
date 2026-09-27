"""`mentee_completed_sessions` on ``GET /api/v1/me``.

Explore shows its match prompt to mentees with two or fewer completed sessions,
so this is the caller's count **as a mentee** — sessions they *received*. The
card's `completed_sessions` counts sessions a mentor *gave*, and a dual-role user
has both, so each test here builds the side that must not count as well as the
one that must.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import add_session, make_bookable_mentor

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]

URL = "/api/v1/me"


async def sign_in(engine: AsyncEngine, user: UUID) -> dict[str, str]:
    """Give an existing user a login, and return the header that uses it.

    The address is replaced too: the factories use `@example.test`, which `/me`
    refuses to serialise as a reserved domain — they were written for public
    reads, where nobody signs in.
    """
    auth_id = uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET auth_id = :a, email = :e WHERE id = :u"),
            {"a": auth_id, "e": f"{auth_id}@example.com", "u": user},
        )
    return bearer(api_token(auth_id))


async def mentee_of(engine: AsyncEngine, session_id: UUID) -> UUID:
    async with engine.begin() as conn:
        mentee = await conn.execute(
            text("SELECT mentee_id FROM sessions WHERE id = :s"), {"s": session_id}
        )
        return UUID(str(mentee.scalar_one()))


async def count(client: httpx.AsyncClient, headers: dict[str, str]) -> object:
    response = await client.get(URL, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["mentee_completed_sessions"]


async def test_only_completed_sessions_the_caller_received_are_counted(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "mentee-count")
    first = await add_session(db_engine, mentor, days_ago=1)
    mentee = await mentee_of(db_engine, first)
    await add_session(db_engine, mentor, mentee=mentee, days_ago=2)
    for n, status in enumerate(("cancelled", "no_show", "declined", "withdrawn"), start=3):
        await add_session(db_engine, mentor, mentee=mentee, status=status, days_ago=n)
    await add_session(db_engine, mentor, mentee=mentee, status="confirmed", days_ago=-2)

    assert await count(api_client, await sign_in(db_engine, mentee)) == 2


async def test_sessions_the_caller_gave_as_a_mentor_do_not_count(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The inversion `delivered()` is tested for, from the other side."""
    mentor = await make_bookable_mentor(db_engine, "mentee-count-gave")
    await add_session(db_engine, mentor, days_ago=1)
    await add_session(db_engine, mentor, days_ago=2)

    assert await count(api_client, await sign_in(db_engine, mentor)) == 0


async def test_a_dual_role_user_counts_only_the_sessions_they_received(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "mentee-count-host")
    dual = await make_bookable_mentor(db_engine, "mentee-count-dual")
    await add_session(db_engine, dual, days_ago=1)  # gave
    await add_session(db_engine, mentor, mentee=dual, days_ago=2)  # received

    assert await count(api_client, await sign_in(db_engine, dual)) == 1


async def test_nobody_s_sessions_is_zero_not_null(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "mentee-count-none")

    assert await count(api_client, await sign_in(db_engine, mentor)) == 0
