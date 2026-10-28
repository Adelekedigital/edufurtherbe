"""`can_message` on a mentor's public profile (decision 302): the send's own
rule, asked ahead. True only for a signed-in mentee connected to this mentor
who could send now; false for everyone else."""

from __future__ import annotations

import datetime as dt

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.message_helpers import Person, book, close_account, pair, person

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def can_message(client: httpx.AsyncClient, mentor: Person, viewer: Person | None) -> bool:
    response = await client.get(
        f"/api/v1/mentors/{mentor.id}", headers=viewer.headers if viewer else {}
    )
    assert response.status_code == 200, response.text
    if viewer is not None:
        assert response.headers["cache-control"] == "private"
    assert "Authorization" in response.headers["vary"]
    value = response.json()["can_message"]
    assert isinstance(value, bool)
    return value


async def test_a_connected_mentee_can_message_their_mentor(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee = await pair(db_engine, "can-yes")

    assert await can_message(api_client, mentor, mentee) is True


async def test_nobody_else_can(api_client: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    mentor, mentee = await pair(db_engine, "can-no")
    stranger = await person(db_engine, "can-no-stranger")
    waiting = await person(db_engine, "can-no-waiting")
    await book(
        db_engine, mentor, waiting, status="pending_mentor_approval", starts_in=dt.timedelta(days=5)
    )

    assert await can_message(api_client, mentor, None) is False
    assert await can_message(api_client, mentor, mentor) is False
    assert await can_message(api_client, mentor, stranger) is False
    assert await can_message(api_client, mentor, waiting) is False
    # Still true for the connected mentee beside them.
    assert await can_message(api_client, mentor, mentee) is True


async def test_a_mentor_viewing_a_mentor_they_mentored_cannot(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Connected the other way round: the viewer is the mentor in their chat,
    so the profile's Message button (a mentee's) is not theirs."""
    mentor = await person(db_engine, "can-mm-a", slug="can-mm-a", mentor="listed")
    their_mentor = await person(db_engine, "can-mm-b", slug="can-mm-b", mentor="listed")
    await book(db_engine, their_mentor, mentor)

    assert await can_message(api_client, mentor, their_mentor) is False
    assert await can_message(api_client, their_mentor, mentor) is True


async def test_a_paused_mentor_cannot_be_messaged(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee = await pair(db_engine, "can-paused")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE mentor_profiles SET listing_status = 'unlisted' WHERE user_id = :u"),
            {"u": mentor.id},
        )
        # Admins and the mentor read a paused profile; give the mentee that
        # view only to ask the question.
        await conn.execute(
            text("INSERT INTO admin_users (user_id, admin_role) VALUES (:u, 'limited_access')"),
            {"u": mentee.id},
        )

    assert await can_message(api_client, mentor, mentee) is False


async def test_a_closed_mentee_account_reads_as_signed_out(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee = await pair(db_engine, "can-closed")
    await close_account(db_engine, mentee)

    assert await can_message(api_client, mentor, mentee) is False
