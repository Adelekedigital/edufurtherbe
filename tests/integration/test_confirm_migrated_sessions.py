"""Upcoming sessions the legacy load brought in are confirmed in the new app
(decision 366): a room, an invite and the four reminders, as `on_confirmed`
gives a booking, and only for the sessions that need it."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from typing import Any
from uuid import UUID

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
from app.infra.clients.meetings import MeetingRoom, VenueUnavailableError
from app.infra.db.session_writer import (
    REMINDER_CALLBACK_PATH,
    FollowUps,
    clear_legacy_venue,
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


async def booked(
    api_client: httpx.AsyncClient, engine: AsyncEngine, tag: str, provider: str = "daily"
) -> str:
    """A confirmed session booked through the API, its venue on fakes."""
    app = api_client._transport.app  # type: ignore[attr-defined]
    app.state.meeting_rooms, app.state.calendar = FakeRooms(), FakeCalendar()
    return str((await book(api_client, await a_mentor_on(engine, tag, provider)))["id"])


class NamedRooms(FakeRooms):
    """Keeps the name it was asked for as the room's handle, as Daily does."""

    def create(self, *, name: str, opens_at: dt.datetime, closes_at: dt.datetime) -> MeetingRoom:
        super().create(name=name, opens_at=opens_at, closes_at=closes_at)
        return MeetingRoom(url=f"https://ef.daily.co/{name}", external_id=name)


class DownRooms(FakeRooms):
    """Daily refusing to make a room."""

    def create(self, *, name: str, opens_at: dt.datetime, closes_at: dt.datetime) -> MeetingRoom:
        del name, opens_at, closes_at
        raise VenueUnavailableError("daily is down")


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
    # No offering: `provision_meeting` gives it no venue, so it is not listed.
    unoffered = await booked(api_client, engine, "cm-unoffered")
    await as_loaded(engine, unoffered)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET session_type_id = NULL WHERE id = :i"), {"i": unoffered}
        )

    found = await listed(engine)

    assert owed in found
    assert not {native, past, cancelled, done, unoffered} & set(found)


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
    assert "confirmed: 1, incomplete: 0" in capsys.readouterr().out


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


async def test_a_meet_offering_drops_the_legacy_room_and_waits_for_its_own_meet(
    api_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Meet makes no room, and `provision_meeting` keeps a stored link: left in
    place, the mentor's reused personal room would be handed out for good."""
    session_id = await booked(api_client, engine, "cm-meet", "google_meet")
    await as_loaded(engine, session_id)
    wired, *_ = follow_ups()

    assert await run(apply=True, engine=engine, follow_ups=wired) == 0

    venue = await venue_of(engine, session_id)
    assert venue["meeting_provider"] == "google_meet"
    assert venue["meeting_url"] is None
    assert venue["external_room_id"] is None
    assert venue["external_calendar_event_id"] == "event-1"


async def test_a_failed_room_is_reported_and_hands_out_no_legacy_link(
    api_client: httpx.AsyncClient, engine: AsyncEngine, capsys: pytest.CaptureFixture[str]
) -> None:
    session_id = await booked(api_client, engine, "cm-noroom")
    await as_loaded(engine, session_id)
    wired, *_ = follow_ups()
    wired = replace(wired, rooms=DownRooms())

    assert await run(apply=True, engine=engine, follow_ups=wired) == 1

    venue = await venue_of(engine, session_id)
    assert venue["meeting_url"] is None
    assert f"{session_id}: no video room" in capsys.readouterr().out


async def test_a_failed_invite_is_reported_and_its_room_is_never_made_twice(
    api_client: httpx.AsyncClient, engine: AsyncEngine, capsys: pytest.CaptureFixture[str]
) -> None:
    session_id = await booked(api_client, engine, "cm-noinvite")
    await as_loaded(engine, session_id)
    rooms = NamedRooms()
    wired, *_ = follow_ups()
    wired = replace(wired, rooms=rooms, calendar=FakeCalendar(refuses_create=True))

    assert await run(apply=True, engine=engine, follow_ups=wired) == 1
    assert f"{session_id}: no calendar invite" in capsys.readouterr().out
    assert (await venue_of(engine, session_id))["external_room_id"] == rooms.calls[0]["name"]

    # A second room under the same name is refused, so it is not listed again.
    assert session_id not in await listed(engine)


async def test_clearing_never_touches_a_venue_the_new_app_gave(
    api_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Scoped in the write itself, not only by the caller's list."""
    native = await booked(api_client, engine, "cm-keep")
    before = await venue_of(engine, native)

    async with async_sessionmaker(engine)() as session:
        await clear_legacy_venue(session, UUID(native), now=dt.datetime.now(dt.UTC))
        await session.commit()

    assert await venue_of(engine, native) == before
    assert before["meeting_url"] is not None
