"""Saving the viewer's timezone on the profile write (booking request item 7).

`users.timezone` existed and `/me` returned it, but nothing wrote it. It is now
writable on `PATCH /users/{id}/profile`, validated by the same IANA check as
availability rules (#36), and a mentor's change still marks their next free time
stale — the availability trigger already watches the column (ADR 0029).
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import make_bookable_mentor
from tests.integration.test_api_writes import make_user, url

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def a_user(engine: AsyncEngine, tag: str) -> tuple[UUID, dict[str, str]]:
    auth_id = uuid4()
    user_id = await make_user(engine, auth_id, f"tz-{tag}@example.com")
    return user_id, bearer(api_token(auth_id))


async def zone_of(engine: AsyncEngine, user_id: UUID) -> str:
    async with engine.connect() as conn:
        return str(
            (
                await conn.execute(text("SELECT timezone FROM users WHERE id = :u"), {"u": user_id})
            ).scalar_one()
        )


async def test_a_user_saves_their_timezone(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await a_user(db_engine, "save")

    response = await api_client.patch(
        url(user_id, "profile"), json={"timezone": "Europe/London"}, headers=headers
    )
    me = (await api_client.get("/api/v1/me", headers=headers)).json()

    assert response.status_code == 204
    assert await zone_of(db_engine, user_id) == "Europe/London"
    assert me["timezone"] == "Europe/London"


async def test_the_name_is_trimmed(api_client: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    user_id, headers = await a_user(db_engine, "trim")

    await api_client.patch(
        url(user_id, "profile"), json={"timezone": "  America/Toronto "}, headers=headers
    )

    assert await zone_of(db_engine, user_id) == "America/Toronto"


@pytest.mark.parametrize("value", ["Mars/Olympus", "+01:00", None, ""])
async def test_anything_but_an_iana_name_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, value: object
) -> None:
    user_id, headers = await a_user(db_engine, f"bad-{uuid4().hex[:6]}")

    response = await api_client.patch(
        url(user_id, "profile"), json={"timezone": value}, headers=headers
    )

    assert response.status_code == 422
    assert "/timezone" in {e["pointer"] for e in response.json()["errors"]}
    assert await zone_of(db_engine, user_id) == "Africa/Lagos"


async def test_leaving_it_out_leaves_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await a_user(db_engine, "omit")

    await api_client.patch(url(user_id, "profile"), json={"about_me": "Hi"}, headers=headers)

    assert await zone_of(db_engine, user_id) == "Africa/Lagos"


async def test_a_timezone_alone_creates_no_empty_profile(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Saving a zone is not starting a profile: `/me` keeps `profile: null`."""
    user_id, headers = await a_user(db_engine, "noprofile")

    await api_client.patch(
        url(user_id, "profile"), json={"timezone": "Asia/Tokyo"}, headers=headers
    )

    async with db_engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT count(*) FROM user_profiles WHERE user_id = :u"), {"u": user_id}
            )
        ).scalar_one()
    assert rows == 0
    assert await zone_of(db_engine, user_id) == "Asia/Tokyo"


async def test_another_users_timezone_cannot_be_written(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    victim, _ = await a_user(db_engine, "victim")
    _, headers = await a_user(db_engine, "attacker")

    response = await api_client.patch(
        url(victim, "profile"), json={"timezone": "Europe/Berlin"}, headers=headers
    )

    assert response.status_code in {403, 404}
    assert await zone_of(db_engine, victim) == "Africa/Lagos"


async def test_a_mentor_changing_zone_marks_their_next_free_time_stale(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Their hours are wall-clock in their zone, so the stored next free time
    is wrong the moment the zone changes: the change log records it (#175)."""
    mentor = await make_bookable_mentor(db_engine, "tz-mentor")
    auth_id = uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET auth_id = :a, email = 'tz-mentor@example.com' WHERE id = :u"),
            {"a": auth_id, "u": mentor},
        )
        await conn.execute(
            text("DELETE FROM mentor_availability_changes WHERE mentor_user_id = :u"),
            {"u": mentor},
        )

    await api_client.patch(
        url(mentor, "profile"),
        json={"timezone": "Pacific/Auckland"},
        headers=bearer(api_token(auth_id)),
    )

    async with db_engine.connect() as conn:
        logged = (
            await conn.execute(
                text("SELECT count(*) FROM mentor_availability_changes WHERE mentor_user_id = :u"),
                {"u": mentor},
            )
        ).scalar_one()
    assert logged >= 1


async def test_a_patch_without_the_timezone_leaves_it_alone(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The positive half of the spec test: sending only the cover keeps the zone."""
    user_id, headers = await a_user(db_engine, "partial")
    await api_client.patch(
        url(user_id, "profile"), json={"timezone": "Asia/Tokyo"}, headers=headers
    )

    response = await api_client.patch(
        url(user_id, "profile"), json={"cover_color": "sky"}, headers=headers
    )

    assert response.status_code == 204, response.text
    assert await zone_of(db_engine, user_id) == "Asia/Tokyo"
