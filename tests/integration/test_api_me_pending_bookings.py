"""`booking_counts` on ``GET /api/v1/me`` — the sidebar's Bookings badge.

`{as_mentor: {awaiting_your_response, upcoming}, as_mentee: {awaiting_mentor,
upcoming}}`. `as_mentor` is null without a mentor profile; `as_mentee` is always
filled, since anyone signed in may book. Each test builds what must not
count beside what must — a lapsed request, a past or cancelled session, and the
caller's sessions in their other role.
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
PENDING, CONFIRMED = "pending_mentor_approval", "confirmed"


async def a_user(engine: AsyncEngine, *, goal: bool = False) -> UUID:
    async with engine.begin() as conn:
        user = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, first_name, primary_role, timezone) "
                    "VALUES (:e, 'Mo', 'mentee', 'UTC') RETURNING id"
                ),
                {"e": f"user-{uuid4()}@example.test"},
            )
        ).scalar_one()
        if goal:
            await conn.execute(text("INSERT INTO mentee_goals (user_id) VALUES (:u)"), {"u": user})
    return user


async def give_goal(engine: AsyncEngine, user: UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO mentee_goals (user_id) VALUES (:u)"), {"u": user})


async def a_session(
    engine: AsyncEngine,
    mentor: UUID,
    mentee: UUID,
    *,
    days_ahead: int,
    status: str = PENDING,
    respond_in: dt.timedelta = dt.timedelta(days=1),
) -> UUID:
    """A session on `mentor`'s first offering; distinct `days_ahead` keep the
    no-double-booking constraint from meeting two live ones."""
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
                    "r": now + respond_in if status == PENDING else None,
                },
            )
        ).scalar_one()


async def counts(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, object]:
    response = await client.get(URL, headers=headers)
    assert response.status_code == 200, response.text
    return dict(response.json()["booking_counts"])


async def test_a_mentor_counts_what_waits_on_them_and_what_is_coming(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "counts-mentor")
    mentee = await a_user(db_engine)
    first = await a_session(db_engine, mentor, mentee, days_ahead=3)
    await a_session(db_engine, mentor, mentee, days_ahead=5)
    await a_session(db_engine, mentor, mentee, days_ahead=7, respond_in=-dt.timedelta(minutes=1))
    await a_session(db_engine, mentor, mentee, days_ahead=9, status=CONFIRMED)
    await a_session(db_engine, mentor, mentee, days_ahead=-2, status=CONFIRMED)  # past
    await a_session(db_engine, mentor, mentee, days_ahead=11, status="cancelled")
    headers = await sign_in(db_engine, mentor)

    before = await counts(api_client, headers)
    accepted = await api_client.post(f"/api/v1/sessions/{first}/accept", headers=headers)
    after = await counts(api_client, headers)

    assert before == {
        "as_mentor": {"awaiting_your_response": 2, "upcoming": 1},
        "as_mentee": {"awaiting_mentor": 0, "upcoming": 0},
    }
    assert accepted.status_code == 200, accepted.text
    assert after["as_mentor"] == {"awaiting_your_response": 1, "upcoming": 2}


async def test_a_mentee_counts_their_own_requests_and_sessions(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "counts-their-mentor")
    mentee = await a_user(db_engine, goal=True)
    await a_session(db_engine, mentor, mentee, days_ahead=3)
    await a_session(db_engine, mentor, mentee, days_ahead=5, respond_in=-dt.timedelta(minutes=1))
    await a_session(db_engine, mentor, mentee, days_ahead=7, status=CONFIRMED)
    await a_session(db_engine, mentor, mentee, days_ahead=-3, status=CONFIRMED)
    headers = await sign_in(db_engine, mentee)

    assert await counts(api_client, headers) == {
        "as_mentor": None,
        "as_mentee": {"awaiting_mentor": 1, "upcoming": 1},
    }


async def test_a_dual_role_user_gets_both_halves_kept_apart(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    both = await make_bookable_mentor(db_engine, "counts-both")
    await give_goal(db_engine, both)
    other = await make_bookable_mentor(db_engine, "counts-other")
    somebody = await a_user(db_engine)
    await a_session(db_engine, both, somebody, days_ahead=3)  # waits on them
    await a_session(db_engine, other, both, days_ahead=5)  # their own request
    await a_session(db_engine, other, both, days_ahead=7, status=CONFIRMED)
    headers = await sign_in(db_engine, both)

    assert await counts(api_client, headers) == {
        "as_mentor": {"awaiting_your_response": 1, "upcoming": 0},
        "as_mentee": {"awaiting_mentor": 1, "upcoming": 1},
    }


async def test_a_mentee_without_a_goal_still_counts_their_requests(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Booking needs no goal, so the badge must not either (app-shell Round 2)."""
    mentor = await make_bookable_mentor(db_engine, "counts-goalless")
    mentee = await a_user(db_engine)
    await a_session(db_engine, mentor, mentee, days_ahead=3)
    headers = await sign_in(db_engine, mentee)

    me = await api_client.get(URL, headers=headers)

    assert me.status_code == 200, me.text
    assert me.json()["booking_counts"] == {
        "as_mentor": None,
        "as_mentee": {"awaiting_mentor": 1, "upcoming": 0},
    }
    assert me.json()["credits"] is not None


async def test_a_mentor_without_a_goal_who_books_sees_their_request(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Any signed-in user may book, a mentor included (Codex on #332)."""
    mentor = await make_bookable_mentor(db_engine, "counts-mentor-books")
    other = await make_bookable_mentor(db_engine, "counts-mentor-booked")
    await a_session(db_engine, other, mentor, days_ahead=3)
    headers = await sign_in(db_engine, mentor)

    me = await api_client.get(URL, headers=headers)

    assert me.status_code == 200, me.text
    assert me.json()["booking_counts"] == {
        "as_mentor": {"awaiting_your_response": 0, "upcoming": 0},
        "as_mentee": {"awaiting_mentor": 1, "upcoming": 0},
    }
    assert me.json()["credits"] is not None


async def test_only_the_mentor_half_needs_a_mentor_profile(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentee = await a_user(db_engine)
    headers = await sign_in(db_engine, mentee)

    assert await counts(api_client, headers) == {
        "as_mentor": None,
        "as_mentee": {"awaiting_mentor": 0, "upcoming": 0},
    }


async def test_a_mentor_cannot_answer_a_request_past_its_deadline(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The badge stops counting a lapsed request, so answering it is refused too —
    otherwise the badge could read zero beside a request still answerable."""
    mentor = await make_bookable_mentor(db_engine, "counts-lapsed")
    mentee = await a_user(db_engine)
    lapsed = await a_session(
        db_engine, mentor, mentee, days_ahead=3, respond_in=-dt.timedelta(minutes=1)
    )
    live = await a_session(db_engine, mentor, mentee, days_ahead=5)
    headers = await sign_in(db_engine, mentor)

    refused = await api_client.post(f"/api/v1/sessions/{lapsed}/accept", headers=headers)
    declined = await api_client.post(f"/api/v1/sessions/{lapsed}/decline", headers=headers)
    accepted = await api_client.post(f"/api/v1/sessions/{live}/accept", headers=headers)

    assert refused.status_code == 409, refused.text
    assert declined.status_code == 409, declined.text
    assert accepted.status_code == 200, accepted.text
