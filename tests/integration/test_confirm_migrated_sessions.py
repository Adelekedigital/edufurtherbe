"""Upcoming sessions the legacy load brought in are confirmed in the new app
(decision 366): a room, an invite and the four reminders, as `on_confirmed`
gives a booking, and only for the sessions that need it."""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
import pytest
from scripts.confirm_migrated_sessions import run
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.meeting_fakes import FakeCalendar, FakeRooms
from tests.integration.scheduler_fakes import FakeScheduler
from tests.integration.test_api_meeting_provisioning import a_mentor_on, book, venue_of

from app.domain.messages import session_page_url
from app.domain.notifications import SESSION_REMINDERS
from app.infra.db.session_writer import (
    REMINDER_CALLBACK_PATH,
    FollowUps,
    migrated_to_confirm,
)

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


@pytest.fixture
def engine(db_engine: AsyncEngine) -> AsyncEngine:
    return db_engine


CALLBACK = f"https://api.example.test{REMINDER_CALLBACK_PATH}"
#: A mentor's personal Meet room as the legacy app stored it: no scheme.
LEGACY_LINK = "meet.google.com/abc-defg-hij"


def follow_ups() -> tuple[FollowUps, FakeRooms, FakeCalendar, FakeScheduler]:
    rooms, calendar, scheduler = FakeRooms(), FakeCalendar(), FakeScheduler()
    wired = FollowUps(
        rooms=rooms,
        calendar=calendar,
        scheduler=scheduler,
        callback_url=CALLBACK,
        app_base_url="https://app.example.test",
    )
    return wired, rooms, calendar, scheduler


async def booked(api_client: httpx.AsyncClient, engine: AsyncEngine, tag: str) -> str:
    """A confirmed session booked through the API, its venue on fakes."""
    app = api_client._transport.app  # type: ignore[attr-defined]
    app.state.meeting_rooms, app.state.calendar = FakeRooms(), FakeCalendar()
    return str((await book(api_client, await a_mentor_on(engine, tag, "daily")))["id"])


async def as_loaded(engine: AsyncEngine, session_id: str, **changes: Any) -> None:
    """Make a session look as the legacy load leaves one: a legacy id, the
    mentor's old link, no room or calendar event of the new app's."""
    values = {
        "legacy": f"legacy-{session_id}",
        "url": LEGACY_LINK,
        "status": "confirmed",
        "event": None,
        "starts": None,
    } | changes
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE sessions SET legacy_bubble_id = :legacy, meeting_provider = NULL,"
                " meeting_url = :url, external_room_id = 'legacyroomtoken0001',"
                " external_calendar_event_id = :event, status = :status,"
                " starts_at = COALESCE(CAST(:starts AS timestamptz), starts_at) WHERE id = :i"
            ),
            values | {"i": session_id},
        )


async def listed(engine: AsyncEngine) -> list[str]:
    async with async_sessionmaker(engine)() as session:
        due = await migrated_to_confirm(session, now=dt.datetime.now(dt.UTC))
    return [str(session_id) for session_id, _ in due]


async def test_only_upcoming_confirmed_migrated_sessions_without_an_event_are_listed(
    api_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    owed = await booked(api_client, engine, "cm-owed")
    await as_loaded(engine, owed)
    # Booked in the new app, its invite failed: no event, but no legacy id.
    native = await booked(api_client, engine, "cm-native")
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET external_calendar_event_id = NULL WHERE id = :i"),
            {"i": native},
        )
    past = await booked(api_client, engine, "cm-past")
    await as_loaded(engine, past, starts=dt.datetime.now(dt.UTC) - dt.timedelta(hours=2))
    cancelled = await booked(api_client, engine, "cm-cancelled")
    await as_loaded(engine, cancelled, status="cancelled")
    done = await booked(api_client, engine, "cm-done")
    await as_loaded(engine, done, event="evt-already")

    found = await listed(engine)

    assert owed in found
    assert not {native, past, cancelled, done} & set(found)


async def test_apply_gives_a_room_an_invite_and_the_reminders_then_lists_none(
    api_client: httpx.AsyncClient, engine: AsyncEngine, capsys: pytest.CaptureFixture[str]
) -> None:
    session_id = await booked(api_client, engine, "cm-apply")
    await as_loaded(engine, session_id)
    wired, rooms, calendar, scheduler = follow_ups()

    assert await run(apply=True, engine=engine, follow_ups=wired) == 0

    venue = await venue_of(engine, session_id)
    assert venue["meeting_provider"] == "daily"
    assert venue["meeting_url"] == "https://ef.daily.co/room"
    assert venue["external_room_id"] == "room-1"
    assert venue["external_calendar_event_id"] == "event-1"
    assert len(rooms.calls) == 1
    assert calendar.calls[0]["join_url"] == session_page_url("https://app.example.test", session_id)
    ours = [p for p in scheduler.published if p["body"]["session_id"] == session_id]
    assert [p["body"]["kind"] for p in ours] == [r.kind for r in SESSION_REMINDERS]
    assert {p["url"] for p in ours} == {CALLBACK}
    assert session_id not in await listed(engine)
    assert "confirmed: " in capsys.readouterr().out


async def test_a_dry_run_lists_and_changes_nothing(
    api_client: httpx.AsyncClient, engine: AsyncEngine, capsys: pytest.CaptureFixture[str]
) -> None:
    session_id = await booked(api_client, engine, "cm-dry")
    await as_loaded(engine, session_id)
    wired, rooms, calendar, scheduler = follow_ups()

    assert await run(apply=False, engine=engine, follow_ups=wired) == 0

    assert session_id in capsys.readouterr().out
    assert (await venue_of(engine, session_id))["meeting_url"] == LEGACY_LINK
    assert not rooms.calls and not calendar.calls and not scheduler.published
    assert session_id in await listed(engine)
