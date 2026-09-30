"""`mentor_pending_bookings` on ``GET /api/v1/me`` — the sidebar's Bookings badge.

The count of requests still waiting on the caller's answer **as a mentor**:
`pending_mentor_approval`, deadline not yet passed. Each test builds what must
not count beside what must — a lapsed request, a confirmed one, and the caller's
own request as somebody else's mentee.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import make_bookable_mentor
from tests.integration.test_api_me_mentee_sessions import sign_in

pytestmark = [pytest.mark.db, pytest.mark.anyio]

URL = "/api/v1/me"


async def a_mentee(engine: AsyncEngine) -> UUID:
    async with engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO users (email, first_name, primary_role, timezone) "
                    "VALUES (:e, 'Mo', 'mentee', 'UTC') RETURNING id"
                ),
                {"e": f"mentee-{uuid4()}@example.test"},
            )
        ).scalar_one()


async def a_request(
    engine: AsyncEngine,
    mentor: UUID,
    mentee: UUID,
    *,
    days_ahead: int,
    status: str = "pending_mentor_approval",
    respond_in: dt.timedelta = dt.timedelta(days=1),
) -> UUID:
    """A session on `mentor`'s first offering, `days_ahead` apart from any other
    so the no-double-booking constraint never meets two of them."""
    now = dt.datetime.now(dt.UTC)
    async with engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO sessions (mentor_id, mentee_id, session_type_id, starts_at, "
                    " duration_minutes, status, respond_by) "
                    "VALUES (:m, :e, "
                    " (SELECT id FROM session_types WHERE mentor_user_id = :m LIMIT 1), "
                    " :s, 45, :st, :r) RETURNING id"
                ),
                {
                    "m": mentor,
                    "e": mentee,
                    "s": now + dt.timedelta(days=days_ahead),
                    "st": status,
                    "r": now + respond_in,
                },
            )
        ).scalar_one()


async def test_a_mentor_counts_only_requests_still_waiting_on_them(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "badge-mentor")
    other = await make_bookable_mentor(db_engine, "badge-other")
    mentee = await a_mentee(db_engine)
    first = await a_request(db_engine, mentor, mentee, days_ahead=3)
    await a_request(db_engine, mentor, mentee, days_ahead=5)
    # Lapsed but not yet swept: no longer the mentor's to answer.
    await a_request(db_engine, mentor, mentee, days_ahead=7, respond_in=-dt.timedelta(minutes=1))
    await a_request(db_engine, mentor, mentee, days_ahead=9, status="confirmed")
    # The caller's own request, as another mentor's mentee, waits on somebody else.
    await a_request(db_engine, other, mentor, days_ahead=11)
    headers = await sign_in(db_engine, mentor)

    before = (await api_client.get(URL, headers=headers)).json()
    accepted = await api_client.post(f"/api/v1/sessions/{first}/accept", headers=headers)
    after = (await api_client.get(URL, headers=headers)).json()

    assert before["mentor_pending_bookings"] == 2
    assert accepted.status_code == 200, accepted.text
    assert after["mentor_pending_bookings"] == 1


async def test_a_mentor_with_nothing_waiting_reads_zero(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "badge-none")
    headers = await sign_in(db_engine, mentor)

    me = (await api_client.get(URL, headers=headers)).json()

    assert me["mentor_pending_bookings"] == 0


async def test_somebody_who_is_not_a_mentor_reads_null(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentee = await a_mentee(db_engine)
    headers = await sign_in(db_engine, mentee)

    me = (await api_client.get(URL, headers=headers)).json()

    assert me["mentor_pending_bookings"] is None
