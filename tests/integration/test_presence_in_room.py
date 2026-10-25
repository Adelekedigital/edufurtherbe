"""Who is in a session's Daily room **now** (#394), from `participant.joined`
and `participant.left` (Session Join PRD, founder decisions 2026-10-21).

`in_room` on each party of the session read: `true` while one of their Daily
joins is open, `false` otherwise, and `null` for a venue that reports no
presence. Webhooks arrive "roughly, but not strictly, in order" and may
repeat, so every ordering below must end the same.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.test_api_meeting_provisioning import a_mentor_on, started
from tests.integration.test_presence_attendance import (
    SECRET,
    WEBHOOK,
    delivery,
    party,
    room_and_users,
    with_webhook_secret,
)

from app.infra.db.room_stays import close_stay, open_stay

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


def joined(room: str, user_id: str, at: dt.datetime, stay: str) -> dict[str, Any]:
    return {
        "type": "participant.joined",
        "id": f"join-{stay}",
        "payload": {
            "room": room,
            "user_id": user_id,
            "joined_at": at.timestamp(),
            "session_id": stay,
        },
    }


def left(
    room: str, user_id: str, at: dt.datetime, stay: str, *, seconds: float = 120.0
) -> dict[str, Any]:
    return {
        "type": "participant.left",
        "id": f"left-{stay}",
        "payload": {
            "room": room,
            "user_id": user_id,
            "joined_at": at.timestamp(),
            "duration": seconds,
            "session_id": stay,
        },
    }


async def send(client: httpx.AsyncClient, event: dict[str, Any]) -> httpx.Response:
    body, headers = delivery(event)
    answered = await client.post(WEBHOOK, content=body, headers=headers)
    assert answered.status_code == 200, answered.text
    return answered


async def in_room(
    client: httpx.AsyncClient, setup: dict[str, Any], session_id: str
) -> tuple[bool | None, bool | None]:
    """`(mentor, mentee)` as the mentee reads the session."""
    shown = await client.get(f"/api/v1/sessions/{session_id}", headers=setup["mentee_headers"])
    assert shown.status_code == 200, shown.text
    body = shown.json()
    return body["mentor"]["in_room"], body["mentee"]["in_room"]


async def a_daily_call(
    client: httpx.AsyncClient, engine: AsyncEngine, tag: str, *, minutes_ago: int = 2
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    with_webhook_secret(client, SECRET)
    setup = await a_mentor_on(engine, tag, "daily")
    session = await started(engine, client, setup, minutes_ago=minutes_ago)
    return setup, session, await room_and_users(engine, session["id"])


@pytest.mark.usefixtures("door")
async def test_a_join_puts_that_party_in_the_room_and_nobody_else(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    setup, session, venue = await a_daily_call(api_client, db_engine, "room-join")
    at = venue["starts_at"] + dt.timedelta(minutes=1)

    assert await in_room(api_client, setup, session["id"]) == (False, False)

    await send(api_client, joined(venue["room"], str(venue["mentee_id"]), at, "tab-1"))

    assert await in_room(api_client, setup, session["id"]) == (False, True)


@pytest.mark.usefixtures("door")
async def test_leaving_takes_them_out_and_a_rejoin_puts_them_back(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    setup, session, venue = await a_daily_call(api_client, db_engine, "room-rejoin")
    mentee = str(venue["mentee_id"])
    at = venue["starts_at"] + dt.timedelta(minutes=1)
    await send(api_client, joined(venue["room"], mentee, at, "tab-1"))

    await send(api_client, left(venue["room"], mentee, at, "tab-1"))

    assert await in_room(api_client, setup, session["id"]) == (False, False)

    await send(api_client, joined(venue["room"], mentee, at + dt.timedelta(minutes=3), "tab-2"))

    assert await in_room(api_client, setup, session["id"]) == (False, True)


@pytest.mark.usefixtures("door")
async def test_a_leave_that_arrives_before_its_join_still_ends_out(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Out of order and repeated: the late join, and a repeat of each, must not
    put back a party who has already gone."""
    setup, session, venue = await a_daily_call(api_client, db_engine, "room-order")
    mentee = str(venue["mentee_id"])
    at = venue["starts_at"] + dt.timedelta(minutes=1)

    await send(api_client, left(venue["room"], mentee, at, "tab-1"))
    await send(api_client, joined(venue["room"], mentee, at, "tab-1"))
    await send(api_client, left(venue["room"], mentee, at, "tab-1"))
    await send(api_client, joined(venue["room"], mentee, at, "tab-1"))

    assert await in_room(api_client, setup, session["id"]) == (False, False)
    async with db_engine.connect() as conn:
        stays = (await conn.execute(text("SELECT count(*) FROM session_room_stays"))).scalar_one()
    assert stays == 1


@pytest.mark.usefixtures("door")
async def test_a_party_in_two_tabs_stays_in_until_both_leave(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    setup, session, venue = await a_daily_call(api_client, db_engine, "room-tabs")
    mentor = str(venue["mentor_id"])
    at = venue["starts_at"] + dt.timedelta(minutes=1)
    await send(api_client, joined(venue["room"], mentor, at, "tab-a"))
    await send(api_client, joined(venue["room"], mentor, at, "tab-b"))

    await send(api_client, left(venue["room"], mentor, at, "tab-a"))

    assert await in_room(api_client, setup, session["id"]) == (True, False)

    await send(api_client, left(venue["room"], mentor, at, "tab-b"))

    assert await in_room(api_client, setup, session["id"]) == (False, False)


@pytest.mark.usefixtures("door")
async def test_nobody_is_in_the_room_once_the_session_has_ended(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**A lost leave must not leave somebody in the room forever.** Daily stops
    sending after three failed deliveries, so an open join is bounded by the
    session's end, when the room closes (`door_window`)."""
    setup, session, venue = await a_daily_call(api_client, db_engine, "room-ended", minutes_ago=61)
    at = venue["starts_at"] + dt.timedelta(minutes=1)
    await send(api_client, joined(venue["room"], str(venue["mentee_id"]), at, "tab-1"))

    assert await in_room(api_client, setup, session["id"]) == (False, False)


@pytest.mark.usefixtures("door")
async def test_a_leave_is_also_evidence_they_were_in_the_room(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A leave carries the join's own time, so a join whose delivery was lost is
    still recorded: `in_room_at` and attendance follow, as for the join."""
    _, session, venue = await a_daily_call(api_client, db_engine, "room-left-only")
    at = venue["starts_at"] + dt.timedelta(minutes=1)

    await send(api_client, left(venue["room"], str(venue["mentee_id"]), at, "tab-1"))

    mentee = await party(db_engine, session["id"], "mentee")
    assert mentee["attendance_status"] == "attended"


@pytest.mark.usefixtures("door")
async def test_a_presence_event_for_anybody_else_records_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Room and user together, as for attendance: our user in another room, or
    a stranger in this one, writes no stay."""
    setup, session, venue = await a_daily_call(api_client, db_engine, "room-other")
    at = venue["starts_at"] + dt.timedelta(minutes=1)

    await send(api_client, joined("ef-daily-not-this-room", str(venue["mentee_id"]), at, "x1"))
    await send(api_client, joined(venue["room"], "0193a5b2-0000-7000-8000-00000000dead", at, "x2"))
    answered = await send(api_client, left(venue["room"], "not-a-uuid", at, "x3"))

    assert answered.json() == {"recorded": False}
    assert await in_room(api_client, setup, session["id"]) == (False, False)
    async with db_engine.connect() as conn:
        stays = (await conn.execute(text("SELECT count(*) FROM session_room_stays"))).scalar_one()
    assert stays == 0


async def test_a_meet_session_reports_no_presence(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Meet sends us nothing, so `null` (unknown), never `false` (not there)."""
    setup = await a_mentor_on(db_engine, "room-meet", "google_meet")
    session = await started(db_engine, api_client, setup, minutes_ago=2)

    assert await in_room(api_client, setup, session["id"]) == (None, None)


@pytest.mark.usefixtures("door")
async def test_the_session_list_carries_it_too(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """One column set for both readers, so a card and the detail agree."""
    setup, session, venue = await a_daily_call(api_client, db_engine, "room-list")
    at = venue["starts_at"] + dt.timedelta(minutes=1)
    await send(api_client, joined(venue["room"], str(venue["mentor_id"]), at, "tab-1"))

    listed = await api_client.get(
        f"/api/v1/users/{venue['mentee_id']}/sessions", headers=setup["mentee_headers"]
    )

    assert listed.status_code == 200, listed.text
    row = next(r for r in listed.json()["data"] if r["id"] == session["id"])
    assert (row["mentor"]["in_room"], row["mentee"]["in_room"]) == (True, False)


@pytest.mark.usefixtures("door")
async def test_a_stay_is_written_only_for_a_party_of_the_session_with_that_room(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**At the writer's own level** (decision 157): every caller today has
    already matched the participant, so only a direct call shows the writer
    refusing a stay for the wrong room or the wrong person on its own."""
    _, _, venue = await a_daily_call(api_client, db_engine, "room-writer")
    at = venue["starts_at"] + dt.timedelta(minutes=1)
    stranger = uuid4()

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as session:
        await open_stay(
            session, room="ef-daily-elsewhere", user_id=venue["mentee_id"], stay="a", joined=at
        )
        await open_stay(session, room=venue["room"], user_id=stranger, stay="b", joined=at)
        await close_stay(
            session,
            room="ef-daily-elsewhere",
            user_id=venue["mentee_id"],
            stay="c",
            joined=at,
            left=at,
        )
        await close_stay(
            session, room=venue["room"], user_id=stranger, stay="d", joined=at, left=at
        )
        await open_stay(
            session, room=venue["room"], user_id=venue["mentee_id"], stay="e", joined=at
        )
        await session.commit()

    async with db_engine.connect() as conn:
        stays = (
            (await conn.execute(text("SELECT provider_session_id FROM session_room_stays")))
            .scalars()
            .all()
        )
    assert stays == ["e"]


@pytest.mark.usefixtures("door")
async def test_nobody_is_in_the_room_of_a_session_that_was_called_off(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A cancelled session's room is not one the door opens (`DOOR_STATUSES`),
    so a stay still open there is not *in the room*."""
    setup, session, venue = await a_daily_call(api_client, db_engine, "room-cancelled")
    at = venue["starts_at"] + dt.timedelta(minutes=1)
    await send(api_client, joined(venue["room"], str(venue["mentee_id"]), at, "tab-1"))

    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET status = 'cancelled' WHERE id = :i"), {"i": session["id"]}
        )

    assert await in_room(api_client, setup, session["id"]) == (False, False)


@pytest.mark.usefixtures("door")
async def test_a_party_whose_account_is_gone_takes_their_presence_with_them(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`null`, as their name and avatar are (decision 212), never a live `true`."""
    setup, session, venue = await a_daily_call(api_client, db_engine, "room-deleted")
    at = venue["starts_at"] + dt.timedelta(minutes=1)
    await send(api_client, joined(venue["room"], str(venue["mentor_id"]), at, "tab-1"))

    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": venue["mentor_id"]}
        )

    assert await in_room(api_client, setup, session["id"]) == (None, False)


@pytest.mark.usefixtures("door")
async def test_a_leave_whose_length_cannot_be_read_still_takes_them_out(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The event says they left; only the length is unknown. And a leave whose
    join never arrived still counts as their arrival."""
    setup, session, venue = await a_daily_call(api_client, db_engine, "room-no-length")
    mentee = str(venue["mentee_id"])
    at = venue["starts_at"] + dt.timedelta(minutes=1)
    await send(api_client, joined(venue["room"], str(venue["mentor_id"]), at, "tab-m"))
    await send(api_client, joined(venue["room"], mentee, at, "tab-1"))
    broken = left(venue["room"], mentee, at, "tab-1")
    del broken["payload"]["duration"]
    lost_join = left(venue["room"], str(venue["mentor_id"]), at, "tab-m2")
    lost_join["payload"]["duration"] = "not a number"

    await send(api_client, broken)
    await send(api_client, lost_join)

    assert await in_room(api_client, setup, session["id"]) == (True, False)
    assert (await party(db_engine, session["id"], "mentee"))["attendance_status"] == "attended"
