"""`avatar_focus` wherever an avatar is sent, not only on explore.

The explore card, featured mentor and public profile got it with #240. A photo
cropped well there and badly on the sessions list or the dashboard would be the
same problem, moved. So every response carrying `avatar_url` carries its focus:
the session's two party cards and the caller's own profile on `/me`.
"""

from __future__ import annotations

from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_sessions import make_session, pair

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def give_focus(engine: AsyncEngine, user: UUID, x: float, y: float) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_profiles (user_id, avatar_url, avatar_focus_x, "
                " avatar_focus_y, avatar_focus_source) "
                "VALUES (:u, 'https://cdn.example/a.jpg', :x, :y, 'detected') "
                "ON CONFLICT (user_id) DO UPDATE SET avatar_url = excluded.avatar_url, "
                " avatar_focus_x = excluded.avatar_focus_x, "
                " avatar_focus_y = excluded.avatar_focus_y, "
                " avatar_focus_source = excluded.avatar_focus_source"
            ),
            {"u": user, "x": x, "y": y},
        )


async def test_both_party_cards_on_a_session_carry_their_focus(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Each party's own point, not one copied to both — the helper that builds
    a party card is shared by prefix, which is exactly where a mix-up hides."""
    mentor, _, mentee, mentee_auth = await pair(db_engine, "focus-parties")
    await give_focus(db_engine, mentor, 0.4, 0.3)
    await give_focus(db_engine, mentee, 0.6, 0.2)
    session_id = await make_session(db_engine, mentor, mentee)

    body = (
        await api_client.get(
            f"/api/v1/sessions/{session_id}", headers=bearer(api_token(mentee_auth))
        )
    ).json()

    assert body["mentor"]["avatar_focus"] == pytest.approx({"x": 0.4, "y": 0.3})
    assert body["mentee"]["avatar_focus"] == pytest.approx({"x": 0.6, "y": 0.2})


async def test_a_party_without_a_point_reads_null(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, _, mentee, mentee_auth = await pair(db_engine, "focus-parties-none")
    session_id = await make_session(db_engine, mentor, mentee)

    body = (
        await api_client.get(
            f"/api/v1/sessions/{session_id}", headers=bearer(api_token(mentee_auth))
        )
    ).json()

    assert body["mentor"]["avatar_focus"] is None


async def test_the_callers_own_profile_on_me_carries_their_focus(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, _, mentee, mentee_auth = await pair(db_engine, "focus-me")
    await give_focus(db_engine, mentee, 0.55, 0.35)
    # The session helpers use `@example.test`, a reserved domain `/me` will not
    # serialise; this test is about the focus, so the user gets a normal address.
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET email = 'focus-me@example.com' WHERE id = :u"), {"u": mentee}
        )

    body = (await api_client.get("/api/v1/me", headers=bearer(api_token(mentee_auth)))).json()

    assert body["profile"]["avatar_focus"] == pytest.approx({"x": 0.55, "y": 0.35})
