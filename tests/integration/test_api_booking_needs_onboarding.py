"""Booking is for someone who has finished onboarding (owner, 2026-10-18).

A signed-in mentee who has not finished picks a time and answers the
questions, is sent to finish setting up, then comes back to confirm. The
refusal carries its own problem type so the client shows that step.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import add_session
from tests.integration.test_api_booking import a_bookable_offering, body, first_slot

from conftest import add_user, api_token, bearer, finish_onboarding, fund, give_mentee_answers

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

URL = "/api/v1/sessions"


async def an_unfinished_mentee(engine: AsyncEngine, tag: str) -> tuple[UUID, dict[str, str]]:
    """Holding credits but not finished: the gate, not the balance, refuses."""
    auth_id = uuid4()
    async with engine.begin() as conn:
        mentee = await add_user(
            conn, f"gate-{tag}@example.test", auth_id=auth_id, primary_role="mentee"
        )
        await fund(conn, mentee, onboard=False)
    return mentee, bearer(api_token(auth_id))


async def book(
    client: httpx.AsyncClient, headers: dict[str, str], session_type: UUID, at: str, key: str
) -> httpx.Response:
    return await client.post(
        URL, json=body(session_type, at), headers=headers | {"Idempotency-Key": key}
    )


async def sessions_of(engine: AsyncEngine, mentee: UUID) -> int:
    async with engine.begin() as conn:
        return int(
            (
                await conn.execute(
                    text("SELECT count(*) FROM sessions WHERE mentee_id = :u"), {"u": mentee}
                )
            ).scalar_one()
        )


async def test_an_unfinished_mentee_is_refused_with_its_own_type_and_what_is_owed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, session_type = await a_bookable_offering(db_engine, "gate-refused")
    mentee, headers = await an_unfinished_mentee(db_engine, "refused")
    at = await first_slot(api_client, mentor, session_type)

    response = await book(api_client, headers, session_type, at, "gate-1")

    assert response.status_code == 409, response.text
    assert response.json()["type"] == "/problems/booking-needs-onboarding"
    assert response.json()["missing"] == ["help", "origin", "degree", "field"]
    assert await sessions_of(db_engine, mentee) == 0


async def test_the_same_key_books_once_onboarding_is_finished(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The refusal stores nothing against the key, so coming back to confirm
    with the same request books rather than replaying the refusal."""
    mentor, session_type = await a_bookable_offering(db_engine, "gate-return")
    mentee, headers = await an_unfinished_mentee(db_engine, "return")
    at = await first_slot(api_client, mentor, session_type)
    assert (await book(api_client, headers, session_type, at, "gate-2")).status_code == 409

    async with db_engine.begin() as conn:
        await finish_onboarding(conn, mentee)
    response = await book(api_client, headers, session_type, at, "gate-2")

    assert response.status_code == 201, response.text
    assert await sessions_of(db_engine, mentee) == 1


async def test_a_migrated_mentee_with_a_completion_date_books_whatever_is_missing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Keyed on `completed_at`: a Bubble completion date counts as finished even
    though today's bar names four answers this mentee never gave."""
    mentor, session_type = await a_bookable_offering(db_engine, "gate-migrated")
    mentee, headers = await an_unfinished_mentee(db_engine, "migrated")
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_onboarding (user_id, completed_at) "
                "VALUES (:u, timestamptz '2024-05-01 00:00:00+00')"
            ),
            {"u": mentee},
        )
    at = await first_slot(api_client, mentor, session_type)

    response = await book(api_client, headers, session_type, at, "gate-3")

    assert response.status_code == 201, response.text


async def test_a_saved_step_is_not_finished(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A row that only records `last_step` is a mentee partway through."""
    mentor, session_type = await a_bookable_offering(db_engine, "gate-partway")
    mentee, headers = await an_unfinished_mentee(db_engine, "partway")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO user_onboarding (user_id, last_step) VALUES (:u, 'plans')"),
            {"u": mentee},
        )
    at = await first_slot(api_client, mentor, session_type)

    response = await book(api_client, headers, session_type, at, "gate-4")

    assert response.status_code == 409, response.text
    assert response.json()["type"] == "/problems/booking-needs-onboarding"


async def test_an_unfinished_mentee_still_reads_and_cancels_an_existing_session(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Only booking is gated: a migrated mentee's existing sessions stay theirs."""
    mentor, _ = await a_bookable_offering(db_engine, "gate-existing")
    mentee, headers = await an_unfinished_mentee(db_engine, "existing")
    session_id = await add_session(
        db_engine,
        mentor,
        mentee=mentee,
        status="confirmed",
        starts_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=3),
        attendance="pending",
    )

    read = await api_client.get(f"{URL}/{session_id}", headers=headers)
    cancelled = await api_client.post(
        f"{URL}/{session_id}/cancel", json={"reason_code": "other"}, headers=headers
    )

    assert read.status_code == 200, read.text
    assert cancelled.status_code == 200, cancelled.text


async def test_a_mentee_who_answered_everything_is_told_only_finishing_is_left(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`missing` is present and empty, not absent: the client's "just finish"
    branch reads it."""
    mentor, session_type = await a_bookable_offering(db_engine, "gate-answered")
    mentee, headers = await an_unfinished_mentee(db_engine, "answered")
    async with db_engine.begin() as conn:
        await conn.execute(text("INSERT INTO user_profiles (user_id) VALUES (:u)"), {"u": mentee})
        await conn.execute(text("INSERT INTO mentee_goals (user_id) VALUES (:u)"), {"u": mentee})
        await give_mentee_answers(conn, mentee)
    at = await first_slot(api_client, mentor, session_type)

    response = await book(api_client, headers, session_type, at, "gate-5")

    assert response.status_code == 409, response.text
    assert response.json()["type"] == "/problems/booking-needs-onboarding"
    assert response.json()["missing"] == []
