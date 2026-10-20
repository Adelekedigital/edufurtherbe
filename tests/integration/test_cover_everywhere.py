"""`cover_color` and `cover_art` wherever an avatar is sent, not only on the profile.

A person's initials sit on their cover's tone (owner, 2026-10-20), so a mentor
who chose a colour must show it on every card: the discovery card (and the
featured, similar and matched cards built from it), a session's two party cards,
and the mentors a mentee has met. A colour shown on the profile and hashed
everywhere else is the same person drawn two ways.
"""

from __future__ import annotations

from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import (
    add_session,
    make_bookable_mentor,
    mentee_of,
    token_for,
)
from tests.integration.test_api_sessions import delete_account, make_session, pair

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def give_cover(engine: AsyncEngine, user: UUID, color: str | None, art: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_profiles (user_id, cover_color, cover_art) "
                "VALUES (:u, :c, :a) ON CONFLICT (user_id) DO UPDATE "
                "SET cover_color = excluded.cover_color, cover_art = excluded.cover_art"
            ),
            {"u": user, "c": color, "a": art},
        )


async def test_the_discovery_card_carries_the_mentors_cover(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "cover-card")
    await give_cover(db_engine, mentor, "lilac", "pattern")

    card = (await api_client.get("/api/v1/mentors")).json()["data"][0]

    assert (card["cover_color"], card["cover_art"]) == ("lilac", "pattern")


async def test_a_card_with_no_chosen_colour_reads_null_so_the_client_hashes(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    await make_bookable_mentor(db_engine, "cover-card-auto")

    card = (await api_client.get("/api/v1/mentors")).json()["data"][0]

    assert (card["cover_color"], card["cover_art"]) == (None, "none")


async def test_each_party_card_carries_its_own_cover(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Each party's own, not one copied to both: the party card is built by
    prefix, which is exactly where a mix-up hides."""
    mentor, _, mentee, mentee_auth = await pair(db_engine, "cover-parties")
    await give_cover(db_engine, mentor, "sage", "icons")
    await give_cover(db_engine, mentee, "peach", "single")
    session_id = await make_session(db_engine, mentor, mentee)

    body = (
        await api_client.get(
            f"/api/v1/sessions/{session_id}", headers=bearer(api_token(mentee_auth))
        )
    ).json()

    assert (body["mentor"]["cover_color"], body["mentor"]["cover_art"]) == ("sage", "icons")
    assert (body["mentee"]["cover_color"], body["mentee"]["cover_art"]) == ("peach", "single")


async def test_a_party_with_no_profile_row_reads_the_automatic_cover(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, _, mentee, mentee_auth = await pair(db_engine, "cover-parties-auto")
    session_id = await make_session(db_engine, mentor, mentee)

    body = (
        await api_client.get(
            f"/api/v1/sessions/{session_id}", headers=bearer(api_token(mentee_auth))
        )
    ).json()

    assert (body["mentee"]["cover_color"], body["mentee"]["cover_art"]) == (None, "none")


async def test_a_deleted_party_loses_their_cover_with_their_name(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, _, mentee, mentee_auth = await pair(db_engine, "cover-gone")
    await give_cover(db_engine, mentor, "rose", "pattern")
    session_id = await make_session(db_engine, mentor, mentee)
    await delete_account(db_engine, mentor)

    party = (
        await api_client.get(
            f"/api/v1/sessions/{session_id}", headers=bearer(api_token(mentee_auth))
        )
    ).json()["mentor"]

    assert party["deleted"] is True
    assert (party["cover_color"], party["cover_art"]) == (None, "none")


async def test_a_mentor_met_carries_their_cover(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "cover-met")
    await give_cover(db_engine, mentor, "aqua", "single")
    mentee = await mentee_of(db_engine, await add_session(db_engine, mentor))

    rows = (
        await api_client.get(
            "/api/v1/me/mentors", headers=bearer(await token_for(db_engine, mentee))
        )
    ).json()["data"]

    assert [(row["cover_color"], row["cover_art"]) for row in rows] == [("aqua", "single")]
