"""The admin Analytics page's scaffold and Credits tab (PRD 31, decision 324).

What carries weight: the gate (switched off and non-admins see nothing), the
exclusions (an admin's or a demo account's money is in no figure), the refund
attributed to the source that **paid** (across a move too), and the alert
thresholds read off real rows.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import admin_token, token_for

from app.core.config import Settings
from app.infra.db.demo_seed import DEMO_DOMAIN
from app.infra.storage.supabase import SupabaseStorage
from conftest import add_user, build_api_app, client_for

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

CREDITS = "/api/v1/admin/analytics/credits"
NOW = dt.datetime.now(dt.UTC)


def app_for(engine: AsyncEngine, storage: SupabaseStorage | None, *, on: bool = True) -> Any:
    return build_api_app(engine, storage, Settings(_env_file=None, admin_analytics_enabled=on))


async def a_user(engine: AsyncEngine, *, email: str | None = None, admin: bool = False) -> UUID:
    async with engine.begin() as conn:
        user = await add_user(
            conn, email or f"{uuid4()}@example.test", primary_role="mentee", timezone="UTC"
        )
        if admin:
            await conn.execute(
                text("INSERT INTO admin_users (user_id, admin_role) VALUES (:u, 'super_admin')"),
                {"u": user},
            )
    return user


async def a_session(
    engine: AsyncEngine, mentor: UUID, mentee: UUID, *, hours: int, moved_from: UUID | None = None
) -> UUID:
    async with engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO sessions (mentor_id, mentee_id, starts_at, duration_minutes, "
                    "status, rescheduled_from_session_id) "
                    "VALUES (:m, :e, :s, 30, 'cancelled', :r) RETURNING id"
                ),
                {"m": mentor, "e": mentee, "s": NOW + dt.timedelta(hours=hours), "r": moved_from},
            )
        ).scalar_one()


async def a_lot(engine: AsyncEngine, user: UUID, source: str, granted: int) -> UUID:
    async with engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO credit_lots (user_id, source, quantity_granted, "
                    "quantity_remaining) VALUES (:u, :s, :q, 0) RETURNING id"
                ),
                {"u": user, "s": source, "q": granted},
            )
        ).scalar_one()


async def move(
    engine: AsyncEngine,
    user: UUID,
    lot: UUID,
    reason: str,
    delta: int,
    *,
    session_id: UUID | None = None,
    days_ago: float = 1,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO credit_transactions "
                "(user_id, credit_lot_id, delta, reason, session_id, created_at) "
                "VALUES (:u, :l, :d, :r, :s, :at)"
            ),
            {
                "u": user,
                "l": lot,
                "d": delta,
                "r": reason,
                "s": session_id,
                "at": NOW - dt.timedelta(days=days_ago),
            },
        )


async def get(client: httpx.AsyncClient, token: str, **query: str) -> httpx.Response:
    return await client.get(CREDITS, params=query, headers={"Authorization": f"Bearer {token}"})


async def test_switched_off_every_route_is_a_404_even_for_an_admin(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    token = await admin_token(db_engine, "super_admin")
    async with client_for(app_for(db_engine, api_storage, on=False)) as client:
        assert (await get(client, token)).status_code == 404
        # Before the token is read: no token is a 404 too, not a 401.
        assert (await client.get(CREDITS)).status_code == 404


async def test_a_non_admin_gets_a_404_and_every_grant_may_read(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    stranger = await token_for(db_engine, await a_user(db_engine))
    async with client_for(app_for(db_engine, api_storage)) as client:
        assert (await get(client, stranger)).status_code == 404
        for role in ("limited_access", "mentor_approval", "super_admin"):
            answer = await get(client, await admin_token(db_engine, role))
            assert answer.status_code == 200, role
            assert answer.headers["Cache-Control"] == "private, no-store"


async def test_a_bad_window_is_a_typed_422_naming_the_parameter(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    token = await admin_token(db_engine)
    async with client_for(app_for(db_engine, api_storage)) as client:
        answer = await get(client, token, **{"from": "2024-05-31", "to": "2024-06-02"})
    assert answer.status_code == 422
    body = answer.json()
    assert body["type"] == "/problems/analytics-window"
    assert body["errors"][0]["pointer"] == "/query/from"


async def test_the_window_is_published_with_its_previous_period(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    token = await admin_token(db_engine)
    async with client_for(app_for(db_engine, api_storage)) as client:
        body = (await get(client, token, **{"from": "2026-11-03", "to": "2026-11-07"})).json()
    assert body["window"] == {
        "from": "2026-11-03",
        "to": "2026-11-07",
        "previous_from": "2026-10-29",
        "previous_to": "2026-11-02",
    }


async def test_credits_count_the_ledger_by_source_and_leave_admins_and_demos_out(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    mentee = await a_user(db_engine)
    mentor = await a_user(db_engine)
    monthly = await a_lot(db_engine, mentee, "monthly_free", 3)
    await move(db_engine, mentee, monthly, "grant", 3)
    booked = await a_session(db_engine, mentor, mentee, hours=24)
    await move(db_engine, mentee, monthly, "session_booked", -1, session_id=booked)
    refund_a = await a_lot(db_engine, mentee, "refund", 1)
    await move(db_engine, mentee, refund_a, "session_cancelled_refund", 1, session_id=booked)
    await move(db_engine, mentee, monthly, "lot_expired", -2)
    # Paid from an admin grant, then moved: the moved session's refund goes to
    # the source its first session spent.
    admin_lot = await a_lot(db_engine, mentee, "admin_grant", 1)
    await move(db_engine, mentee, admin_lot, "grant", 1)
    first = await a_session(db_engine, mentor, mentee, hours=48)
    await move(db_engine, mentee, admin_lot, "session_booked", -1, session_id=first)
    moved = await a_session(db_engine, mentor, mentee, hours=72, moved_from=first)
    refund_b = await a_lot(db_engine, mentee, "refund", 1)
    await move(db_engine, mentee, refund_b, "request_unfulfilled", 1, session_id=moved)
    level = await a_lot(db_engine, mentee, "level_top_up", 1)
    await move(db_engine, mentee, level, "grant", 1)
    # Outside the 7-day window.
    await move(db_engine, mentee, monthly, "grant", 4, days_ago=20)
    # Left out: an admin's and a demo account's movements.
    for who in (
        await a_user(db_engine, admin=True),
        await a_user(db_engine, email=f"d-{uuid4()}@{DEMO_DOMAIN}"),
    ):
        lot = await a_lot(db_engine, who, "monthly_free", 5)
        await move(db_engine, who, lot, "grant", 5)

    token = await admin_token(db_engine)
    async with client_for(app_for(db_engine, api_storage)) as client:
        body = (await get(client, token, window="7d")).json()

    assert body["flow"] == {"granted": 5, "spent": 2, "refunded": 2, "expired": 2}
    rows = {row.pop("source"): row for row in body["by_source"]}
    assert list(rows) == ["monthly", "level_up", "admin", "refund", "carried_over"]
    assert rows["monthly"] == {"granted": 3, "spent": 1, "refunded": 1, "expired": 2}
    assert rows["admin"] == {"granted": 1, "spent": 1, "refunded": 1, "expired": 0}
    assert rows["level_up"] == {"granted": 1, "spent": 0, "refunded": 0, "expired": 0}
    assert rows["refund"] == {"granted": 2, "spent": 0, "refunded": 0, "expired": 0}
    assert rows["carried_over"] == {"granted": 0, "spent": 0, "refunded": 0, "expired": 0}


async def test_quiet_platform_raises_both_quiet_alerts_newest_first(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    token = await admin_token(db_engine)
    # A request two days old, and a sign-up three days old (admins are not
    # sign-ups); a migrated account created today is not a sign-up either.
    mentee = await a_user(db_engine)
    mentor = await a_user(db_engine)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET created_at = now() - interval '3 days' WHERE id IN (:a, :b)"),
            {"a": mentee, "b": mentor},
        )
        await conn.execute(
            text(
                "INSERT INTO sessions (mentor_id, mentee_id, starts_at, duration_minutes, "
                "created_at) VALUES (:m, :e, now() + interval '2 days', 30, "
                "now() - interval '2 days')"
            ),
            {"m": mentor, "e": mentee},
        )
        first = (
            await conn.execute(text("SELECT id FROM sessions WHERE mentee_id = :e"), {"e": mentee})
        ).scalar_one()
        await add_user(conn, f"{uuid4()}@example.test", legacy_bubble_id=f"legacy-{uuid4()}")
    # Made today, and still no request: a move, and a session with an admin.
    await a_session(db_engine, mentor, mentee, hours=96, moved_from=first)
    await a_session(db_engine, await a_user(db_engine, admin=True), mentee, hours=120)
    async with client_for(app_for(db_engine, api_storage)) as client:
        alerts = (await get(client, token)).json()["alerts"]
    assert [x["key"] for x in alerts] == ["no_booking_requests", "no_signups"]
    assert alerts[0]["link"] == {"tab": "sessions", "funnel": None, "by": None}


async def test_a_recent_request_and_sign_up_raise_nothing(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    token = await admin_token(db_engine)
    mentor, mentee = await a_user(db_engine), await a_user(db_engine)
    await a_session(db_engine, mentor, mentee, hours=30)
    async with client_for(app_for(db_engine, api_storage)) as client:
        assert (await get(client, token)).json()["alerts"] == []


@pytest.mark.parametrize(("failed", "fires"), [(20, False), (21, True)])
async def test_more_than_twenty_failures_yesterday_raise_the_delivery_alert(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None, failed: int, fires: bool
) -> None:
    token = await admin_token(db_engine)
    mentor, mentee = await a_user(db_engine), await a_user(db_engine)
    await a_session(db_engine, mentor, mentee, hours=30)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO outbox_events (event_type, entity_type, entity_id, status, "
                "updated_at, destination) SELECT 'x', 'session', gen_random_uuid(), 'failed', "
                "date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' "
                "- interval '12 hours', 'email' FROM generate_series(1, :n)"
            ),
            {"n": failed},
        )
        # Failed today, and a pending row yesterday: neither counts.
        await conn.execute(
            text(
                "INSERT INTO outbox_events (event_type, entity_type, entity_id, status, "
                "updated_at, destination) VALUES "
                "('x', 'session', gen_random_uuid(), 'failed', now(), 'email'), "
                "('x', 'session', gen_random_uuid(), 'pending', now() - interval '1 day', 'email')"
            )
        )
    async with client_for(app_for(db_engine, api_storage)) as client:
        keys = [x["key"] for x in (await get(client, token)).json()["alerts"]]
    assert keys == (["delivery_failed"] if fires else [])
