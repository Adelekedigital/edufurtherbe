"""The admin Analytics Overview and Sessions tabs (PRD 31, decision 324).

One small platform, built by hand, whose every figure is worked out in the
comments: five requests (one moved, then held), decisions an hour apart,
a no-show with a reason, a review, a chat answered in three hours, and an
admin's session that no figure may count.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import admin_token

from app.core.config import Settings
from app.infra.storage.supabase import SupabaseStorage
from conftest import add_user, build_api_app, client_for

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

ROOT = "/api/v1/admin/analytics"
NOW = dt.datetime.now(dt.UTC)


def ago(days: float = 0, hours: float = 0) -> dt.datetime:
    return NOW - dt.timedelta(days=days, hours=hours)


async def person(conn: Any, *, admin: bool = False) -> UUID:
    user = await add_user(conn, f"{uuid4()}@example.test", primary_role="mentee", timezone="UTC")
    if admin:
        await conn.execute(
            text("INSERT INTO admin_users (user_id, admin_role) VALUES (:u, 'super_admin')"),
            {"u": user},
        )
    return user


async def session_row(
    conn: Any,
    mentor: UUID,
    mentee: UUID,
    *,
    status: str,
    created: dt.datetime,
    starts: dt.datetime,
    moved_from: UUID | None = None,
) -> UUID:
    return (
        await conn.execute(
            text(
                "INSERT INTO sessions (mentor_id, mentee_id, starts_at, duration_minutes, status, "
                "created_at, rescheduled_from_session_id) "
                "VALUES (:m, :e, :s, 30, :st, :c, :r) RETURNING id"
            ),
            {
                "m": mentor,
                "e": mentee,
                "s": starts,
                "st": status,
                "c": created,
                "r": moved_from,
            },
        )
    ).scalar_one()


async def decision(conn: Any, session: UUID, to: str, at: dt.datetime) -> None:
    await conn.execute(
        text(
            "INSERT INTO session_events (session_id, from_status, to_status, actor_type, "
            "created_at) VALUES (:s, 'pending_mentor_approval', :t, 'system', :at)"
        ),
        {"s": session, "t": to, "at": at},
    )


@pytest_asyncio.fixture
async def platform(db_engine: AsyncEngine) -> dict[str, UUID]:
    async with db_engine.begin() as conn:
        mentor, ada, bo, admin = (
            await person(conn),
            await person(conn),
            await person(conn),
            await person(conn, admin=True),
        )
        # R1: made 2 days ago, accepted 6 hours later, held yesterday.
        r1 = await session_row(conn, mentor, ada, status="completed", created=ago(2), starts=ago(1))
        await decision(conn, r1, "confirmed", ago(2) + dt.timedelta(hours=6))
        # R2: made 2 days ago, declined 2 hours later.
        r2 = await session_row(conn, mentor, bo, status="declined", created=ago(2), starts=ago(-3))
        await decision(conn, r2, "declined", ago(2) + dt.timedelta(hours=2))
        # R3: made 3 days ago, expired unanswered.
        r3 = await session_row(conn, mentor, bo, status="expired", created=ago(3), starts=ago(-4))
        await decision(conn, r3, "expired", ago(2, 12))
        # R4: made 3 days ago, moved, and the move was held 2 days ago.
        r4 = await session_row(
            conn, mentor, ada, status="withdrawn", created=ago(3), starts=ago(-5)
        )
        await session_row(
            conn,
            mentor,
            ada,
            status="completed",
            created=ago(2, 20),
            starts=ago(2),
            moved_from=r4,
        )
        # R5: made 4 days ago, confirmed at once, and Bo did not come.
        r5 = await session_row(conn, mentor, bo, status="no_show", created=ago(4), starts=ago(1, 3))
        await conn.execute(
            text(
                "INSERT INTO session_missed_reasons (session_id, user_id, reason) "
                "VALUES (:s, :u, 'forgot')"
            ),
            {"s": r5, "u": bo},
        )
        # R5 was confirmed at creation: its creation event is not a decision.
        await conn.execute(
            text(
                "INSERT INTO session_events (session_id, from_status, to_status, actor_type, "
                "created_at) VALUES (:s, NULL, 'confirmed', 'user', :at)"
            ),
            {"s": r5, "at": ago(4)},
        )
        # Left out: a session with an admin, made and held in the window, and
        # the reason somebody gave for missing it.
        with_admin = await session_row(
            conn, admin, ada, status="completed", created=ago(1), starts=ago(0, 5)
        )
        await conn.execute(
            text(
                "INSERT INTO session_missed_reasons (session_id, user_id, reason) "
                "VALUES (:s, :u, 'technical')"
            ),
            {"s": with_admin, "u": ada},
        )
        # One review of the mentor, an overall 4, written yesterday.
        await conn.execute(
            text(
                "INSERT INTO reviews (reviewed_by, reviewed_for, communication_rating, "
                "knowledge_rating, practicality_rating, support_rating, valuable_rating, "
                "overall_rating, nps_recommend_score, public_review, created_at) "
                "VALUES (:by, :for_, 3, 3, 3, 3, 3, 4, 9, 'Fine.', :at)"
            ),
            {"by": ada, "for_": mentor, "at": ago(1)},
        )
        # A chat Ada began 2 days ago, answered by the mentor 3 hours later.
        chat = (
            await conn.execute(
                text(
                    "INSERT INTO conversations (status, mentor_user_id, mentee_user_id) "
                    "VALUES ('accepted', :m, :e) RETURNING id"
                ),
                {"m": mentor, "e": ada},
            )
        ).scalar_one()
        for sender, at in ((ada, ago(2)), (mentor, ago(2) + dt.timedelta(hours=3))):
            await conn.execute(
                text(
                    "INSERT INTO messages (conversation_id, sender_id, message_type, body, "
                    "sent_at) VALUES (:c, :s, 'text', 'Hi', :at)"
                ),
                {"c": chat, "s": sender, "at": at},
            )
        # A chat Bo began yesterday and wrote in again: no reply from the mentor.
        quiet = (
            await conn.execute(
                text(
                    "INSERT INTO conversations (status, mentor_user_id, mentee_user_id) "
                    "VALUES ('accepted', :m, :e) RETURNING id"
                ),
                {"m": mentor, "e": bo},
            )
        ).scalar_one()
        for at in (ago(1), ago(1) + dt.timedelta(hours=1)):
            await conn.execute(
                text(
                    "INSERT INTO messages (conversation_id, sender_id, message_type, body, "
                    "sent_at) VALUES (:c, :s, 'text', 'Hi', :at)"
                ),
                {"c": quiet, "s": bo, "at": at},
            )
        # A migrated mentee created today: not a sign-up.
        await add_user(
            conn,
            f"{uuid4()}@example.test",
            primary_role="mentee",
            timezone="UTC",
            legacy_bubble_id=f"legacy-{uuid4()}",
        )
    return {"mentor": mentor, "ada": ada, "bo": bo}


async def read(
    engine: AsyncEngine, storage: SupabaseStorage | None, tab: str, **query: str
) -> dict[str, Any]:
    app = build_api_app(engine, storage, Settings(_env_file=None, admin_analytics_enabled=True))
    token = await admin_token(engine)
    async with client_for(app) as client:
        answer: httpx.Response = await client.get(
            f"{ROOT}/{tab}", params=query, headers={"Authorization": f"Bearer {token}"}
        )
    assert answer.status_code == 200, answer.text
    assert answer.headers["Cache-Control"] == "private, no-store"
    return answer.json()


@pytest.mark.usefixtures("platform")
async def test_sessions_tab_counts_requests_decisions_and_outcomes(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    body = await read(db_engine, api_storage, "sessions", window="7d")
    kpis = body["kpis"]
    # Five requests: the move and the admin's session are not requests.
    assert kpis["requests"] == {"value": 5, "previous": 0}
    # Accepted 1 of 3 decided (accepted, declined, expired).
    assert kpis["acceptance_rate"]["value"] == 33.3
    # Replies took 6 and 2 hours.
    assert kpis["median_reply_hours"]["value"] == 4.0
    # Held: R1 and the move; no-show: R5.
    assert kpis["show_up_rate"]["value"] == 66.7
    # Ada held two, and nobody else held any.
    assert kpis["repeat_booking_rate"]["value"] == 100.0
    assert kpis["average_rating"] == {"value": 4.0, "previous": None}
    assert kpis["review_rate"]["value"] == 50.0
    assert kpis["reports"]["value"] == 0
    # R4 became completed through its move.
    assert body["outcomes"] == {
        "requests": 5,
        "completed": 2,
        "cancelled": 0,
        "expired": 1,
        "no_show": 1,
        "declined": 1,
    }
    assert body["top_no_show_reason"] == {"reason": "forgot", "share": 100.0}
    guard = body["guardrails"]
    assert guard["no_show_rate"]["value"] == 33.3
    assert guard["request_expiry_rate"]["value"] == 33.3
    assert guard["credit_expiry_rate"] == {"value": None, "previous": None}


@pytest.mark.usefixtures("platform")
async def test_compare_false_nulls_every_previous(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    body = await read(db_engine, api_storage, "sessions", window="7d", compare="false")
    assert all(metric["previous"] is None for metric in body["kpis"].values())
    overview = await read(db_engine, api_storage, "overview", window="7d", compare="false")
    assert overview["north_star"]["completed"]["previous"] is None
    assert all(point["previous"] is None for point in overview["north_star"]["series"])


@pytest.mark.usefixtures("platform")
async def test_overview_north_star_sub_tracks_and_features(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    body = await read(db_engine, api_storage, "overview", window="7d")
    star = body["north_star"]
    assert star["completed"] == {"value": 2, "previous": 0}
    assert star["unit"] == "day"
    assert len(star["series"]) == 7
    assert sum(point["value"] for point in star["series"]) == 2
    assert all(point["previous"] == 0 for point in star["series"])
    subs = body["sub_tracks"]
    assert subs["unique_mentees"]["value"] == 1
    assert subs["mentors_delivered"]["value"] == 1
    assert subs["sessions_per_mentee"]["value"] == 2.0

    features = {f["key"]: f for f in body["features"]}
    assert [f["key"] for f in body["features"]] == [
        "booking",
        "explore",
        "messaging",
        "mentor_supply",
        "invites",
        "mentee_activation",
        "mentor_activation",
    ]
    # Two of five requests became completed sessions.
    assert features["booking"]["value"] == {"value": 40.0, "previous": None}
    assert len(features["booking"]["spark"]) == 8
    assert features["explore"] == {
        "key": "explore",
        "value": None,
        "spark": None,
        "contribution": None,
    }
    # Two chats begun; only Ada's was answered by the other person.
    assert features["messaging"]["value"]["value"] == 50.0
    # Confirmed in the window: R1 (a message 6 hours before) and R5 (none).
    assert features["messaging"]["contribution"] == 50.0
    assert features["mentor_supply"]["spark"] is None
    # Signed up in the window: the mentor, Ada and Bo (the admin and the
    # migrated mentee are not sign-ups); Ada had a completed session.
    assert features["mentee_activation"]["value"]["value"] == 33.3
    assert features["invites"]["value"] == {"value": None, "previous": None}
    assert features["mentor_activation"]["value"] == {"value": None, "previous": None}
    assert features["mentor_supply"]["value"]["previous"] is None


@pytest.mark.usefixtures("platform")
async def test_a_message_after_confirmation_is_not_a_message_first(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    async with db_engine.begin() as conn:
        await conn.execute(text("UPDATE messages SET sent_at = now()"))
    body = await read(db_engine, api_storage, "overview", window="7d")
    features = {f["key"]: f for f in body["features"]}
    assert features["messaging"]["contribution"] == 0.0


async def test_fifteen_days_draw_by_seven_day_buckets(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    today = NOW.date()
    first = (today - dt.timedelta(days=14)).isoformat()
    body = await read(
        db_engine, api_storage, "overview", **{"from": first, "to": today.isoformat()}
    )
    star = body["north_star"]
    assert star["unit"] == "week"
    assert [point["start"] for point in star["series"]] == [
        first,
        (today - dt.timedelta(days=7)).isoformat(),
        today.isoformat(),
    ]
