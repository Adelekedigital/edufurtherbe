"""`sessions.entry_point` (PRD 30 PR 2): stored from the booking, carried on its
events by the live writers and by the backfill alike, and kept by a move.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.factories import add_session, make_public_mentor
from tests.integration.test_analytics_backfill import FakeSink, session_event, session_row
from tests.integration.test_analytics_events import about, events
from tests.integration.test_api_admin_credits import a_user
from tests.integration.test_api_booking import a_bookable_offering, a_mentee, body, key
from tests.integration.test_api_session_move import a_booking as a_movable_booking
from tests.integration.test_api_session_move import later_slots, move
from tests.integration.test_api_session_transitions import a_booking, url

from app.infra.analytics_backfill import backfill_analytics
from app.infra.db.session_writer.attendance import settle_attendance
from conftest import A_REASON, default_ladder

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

AT = dt.datetime(2026, 11, 20, 14, 0, tzinfo=dt.UTC)


async def entry_point_of(engine: AsyncEngine, session_id: Any) -> str | None:
    async with engine.connect() as conn:
        return await conn.scalar(
            text("SELECT entry_point FROM sessions WHERE id = :s"), {"s": session_id}
        )


async def set_entry_point(engine: AsyncEngine, session_id: Any, value: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET entry_point = :v WHERE id = :s"),
            {"v": value, "s": session_id},
        )


async def test_a_booking_stores_its_entry_point_and_both_events_carry_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(
        db_engine, api_client, "ep-auto", confirmed=True, entry_point="explore"
    )

    (requested,) = await events(db_engine, "booking_requested")
    (confirmed,) = await events(db_engine, "session_confirmed")

    assert await entry_point_of(db_engine, booking["id"]) == "explore"
    assert requested["properties"]["entry_point"] == "explore"
    assert confirmed["properties"] == {"session_id": booking["id"], "entry_point": "explore"}


async def test_an_accept_and_a_cancel_carry_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(db_engine, api_client, "ep-accept", entry_point="home")

    accepted = await api_client.post(url(booking, "accept"), headers=booking["mentor"])
    cancelled = await api_client.post(
        url(booking, "cancel"), json=A_REASON, headers=booking["mentee"]
    )

    assert accepted.status_code == cancelled.status_code == 200
    (confirmed,) = await events(db_engine, "session_confirmed")
    (ended,) = await events(db_engine, "session_cancelled")
    assert confirmed["properties"]["entry_point"] == "home"
    assert ended["properties"] == {
        "session_id": booking["id"],
        "by": "mentee",
        "reason_code": A_REASON["reason_code"],
        "entry_point": "home",
    }


async def test_a_booking_without_one_stores_none_and_its_events_carry_no_key(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(db_engine, api_client, "ep-none", confirmed=True)

    assert await entry_point_of(db_engine, booking["id"]) is None
    for payload in about(await events(db_engine), booking["id"]):
        assert "entry_point" not in payload["properties"]


async def test_an_unknown_entry_point_still_books(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(
        db_engine, api_client, "ep-unknown", confirmed=True, entry_point="messages"
    )

    assert await entry_point_of(db_engine, booking["id"]) is None


async def test_settlement_carries_it(db_engine: AsyncEngine) -> None:
    mentor = await make_public_mentor(db_engine, "ep-settle")
    held = await add_session(db_engine, mentor, status="confirmed", mentee_attendance="attended")
    await set_entry_point(db_engine, held, "mentor_profile")

    async with async_sessionmaker(db_engine)() as session:
        assert await settle_attendance(session, now=AT, ladder=default_ladder()) == 1
        await session.commit()

    (completed,) = await events(db_engine, "session_completed")
    assert completed["properties"] == {"session_id": str(held), "entry_point": "mentor_profile"}


async def test_a_move_keeps_the_originals_entry_point(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_movable_booking(api_client, db_engine, "ep-move")
    await set_entry_point(db_engine, booking["id"], "rebook_after_review")

    moved = await move(api_client, booking, booking["starts"][1])

    assert moved.status_code == 201, moved.text
    assert await entry_point_of(db_engine, moved.json()["id"]) == "rebook_after_review"


async def test_the_backfill_carries_it_from_the_same_builders(
    db_engine: AsyncEngine, tmp_path: Path
) -> None:
    mentor = await make_public_mentor(db_engine, "ep-backfill", listed=False)
    mentee: UUID = await a_user(db_engine)
    t0 = dt.datetime(2026, 5, 1, 9, 0, tzinfo=dt.UTC)
    async with db_engine.begin() as conn:
        type_id = await conn.scalar(
            text("SELECT id FROM session_types WHERE mentor_user_id = :m LIMIT 1"), {"m": mentor}
        )
        sid = await session_row(conn, mentor, mentee, type_id, "cancelled", day=1)
        await conn.execute(
            text("UPDATE sessions SET entry_point = 'explore' WHERE id = :s"), {"s": sid}
        )
        await session_event(conn, sid, None, "confirmed", t0)
        await session_event(
            conn,
            sid,
            "confirmed",
            "cancelled",
            t0 + dt.timedelta(hours=1),
            actor=mentee,
            reason_code="other",
        )
    sink = FakeSink()

    await backfill_analytics(
        db_engine,
        sink=sink,
        cursor=tmp_path / "c.json",
        until=dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
        target="a" * 64,
    )

    mine = [e for e in sink.events if e["properties"].get("session_id") == str(sid)]
    assert {e["event"] for e in mine} == {
        "booking_requested",
        "session_confirmed",
        "session_cancelled",
    }
    assert all(e["properties"]["entry_point"] == "explore" for e in mine)


async def test_a_retry_from_another_screen_replays_the_booking(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The hint is not part of the idempotency fingerprint, so it never fails a booking."""
    mentor, session_type = await a_bookable_offering(db_engine, "ep-retry")
    _, headers = await a_mentee(db_engine, "ep-retry")
    starts = await later_slots(api_client, mentor, session_type)
    same = headers | key()

    first = await api_client.post(
        "/api/v1/sessions",
        json=body(session_type, starts[0]) | {"entry_point": "explore"},
        headers=same,
    )
    retry = await api_client.post(
        "/api/v1/sessions",
        json=body(session_type, starts[0]) | {"entry_point": "home"},
        headers=same,
    )

    assert first.status_code == 201, first.text
    assert retry.status_code == 201, retry.text
    assert retry.json()["id"] == first.json()["id"]
    assert await entry_point_of(db_engine, first.json()["id"]) == "explore"
