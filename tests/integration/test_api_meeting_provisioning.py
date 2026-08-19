"""What a confirmed session gets, and which provider it asks.

**The orchestration is ours; the calls are not built.** So these drive real
bookings through the real confirmation paths with a fake room provider and a
fake calendar, and assert what was *asked for* — which is the half that has gone
wrong twice already in this codebase's integrations, silently both times.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import add_availability, add_session_type, make_public_mentor

from app.infra.clients.meetings import CalendarEvent, MeetingRoom
from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

CUSTOM_URL = "https://mentor.example.test/room"


@dataclass
class FakeRooms:
    """Records what it was asked to make."""

    calls: list[dict[str, Any]] = field(default_factory=list)

    def create(self, *, name: str, opens_at: dt.datetime, closes_at: dt.datetime) -> MeetingRoom:
        self.calls.append({"name": name, "opens_at": opens_at, "closes_at": closes_at})
        return MeetingRoom(url="https://ef.daily.co/room", external_id="room-1")


@dataclass
class FakeCalendar:
    """Records whether a conference was requested, which is the whole point."""

    hands_back_a_link: bool = False
    calls: list[dict[str, Any]] = field(default_factory=list)

    def create_event(self, **kwargs: Any) -> CalendarEvent:
        self.calls.append(kwargs)
        return CalendarEvent(
            external_id="event-1",
            meeting_url=("https://meet.google.com/abc" if self.hands_back_a_link else None),
        )


async def a_mentor_on(
    engine: AsyncEngine, tag: str, provider: str | None, *, confirmation: bool = False
) -> dict[str, Any]:
    """A bookable mentor whose default venue is `provider`, or who has none."""
    mentor = await make_public_mentor(engine, tag)
    session_type = await add_session_type(engine, mentor, duration=60, notice=0)
    for day in range(7):
        await add_availability(engine, mentor, day_of_week=day, start="00:00", end="23:00")
    mentor_auth, mentee_auth = uuid4(), uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET auth_id = :a WHERE id = :u"), {"a": mentor_auth, "u": mentor}
        )
        await conn.execute(
            text(
                "INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
                "VALUES (:e, :a, 'Mo', 'mentee', 'Africa/Lagos')"
            ),
            {"e": f"mentee-{tag}@example.test", "a": mentee_auth},
        )
        if confirmation:
            await conn.execute(
                text(
                    "UPDATE mentor_profiles SET requires_booking_confirmation = true "
                    "WHERE user_id = :u"
                ),
                {"u": mentor},
            )
        if provider is not None:
            # The symmetric CHECK refuses `custom` without a URL and refuses a
            # URL on anything else, so the pair moves together.
            await conn.execute(
                text(
                    "INSERT INTO mentor_conferencing_options "
                    "(user_id, provider, is_default, custom_url) "
                    "VALUES (:u, :p, true, :url)"
                ),
                {
                    "u": mentor,
                    "p": provider,
                    "url": CUSTOM_URL if provider == "custom" else None,
                },
            )
    return {
        "mentor": mentor,
        "session_type": session_type,
        "mentee_headers": bearer(api_token(mentee_auth)),
        "mentor_headers": bearer(api_token(mentor_auth)),
    }


async def book(client: httpx.AsyncClient, setup: dict[str, Any]) -> dict[str, Any]:
    slots = await client.get(
        f"/api/v1/users/{setup['mentor']}/availability/slots",
        params={"session_type_id": str(setup["session_type"])},
    )
    created = await client.post(
        "/api/v1/sessions",
        json={
            "session_type_id": str(setup["session_type"]),
            "starts_at": str(slots.json()["data"][-1]["start"]),
        },
        headers=setup["mentee_headers"] | {"Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 201, created.text
    return dict(created.json())


async def venue_of(engine: AsyncEngine, session_id: str) -> dict[str, Any]:
    async with engine.connect() as conn:
        return dict(
            (
                await conn.execute(
                    text(
                        "SELECT meeting_provider, meeting_url, external_room_id, "
                        "external_calendar_event_id FROM sessions WHERE id = :i"
                    ),
                    {"i": session_id},
                )
            )
            .mappings()
            .one()
        )


@pytest.fixture
def fakes(api_client: httpx.AsyncClient) -> tuple[FakeRooms, FakeCalendar]:
    """Wired onto `app.state`, the way a real adapter would be."""
    rooms, calendar = FakeRooms(), FakeCalendar()
    app = api_client._transport.app  # type: ignore[attr-defined]
    app.state.meeting_rooms = rooms
    app.state.calendar = calendar
    return rooms, calendar


# --------------------------------------------------------------------------
# Which provider is asked, and for what
# --------------------------------------------------------------------------


async def test_meet_asks_the_calendar_for_a_conference_and_creates_no_room(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fakes: tuple[FakeRooms, FakeCalendar]
) -> None:
    """One call, not two, and the link comes back on the event."""
    rooms, calendar = fakes
    calendar.hands_back_a_link = True
    setup = await a_mentor_on(db_engine, "mp-meet", "google_meet")

    session = await book(api_client, setup)

    assert rooms.calls == []
    assert calendar.calls[0]["wants_conference"] is True
    stored = await venue_of(db_engine, session["id"])
    assert stored["meeting_url"] == "https://meet.google.com/abc"
    assert stored["external_calendar_event_id"] == "event-1"
    assert stored["external_room_id"] is None


async def test_daily_creates_a_room_and_must_not_ask_for_a_conference(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fakes: tuple[FakeRooms, FakeCalendar]
) -> None:
    """**The failure with no error message.**

    Asking Google for a conference on a session held in Daily puts two links on
    the event, and the invitee clicks whichever the client renders first.
    Nothing errors, and nobody finds out until somebody joins the wrong room.
    """
    rooms, calendar = fakes
    setup = await a_mentor_on(db_engine, "mp-daily", "daily")

    session = await book(api_client, setup)

    assert len(rooms.calls) == 1
    assert calendar.calls[0]["wants_conference"] is False
    stored = await venue_of(db_engine, session["id"])
    assert stored["meeting_url"] == "https://ef.daily.co/room"
    assert stored["external_room_id"] == "room-1"


async def test_the_room_outlives_the_join_window(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fakes: tuple[FakeRooms, FakeCalendar]
) -> None:
    """A room that shut when the window shuts would evict everybody fifteen
    minutes into an hour-long session."""
    rooms, _ = fakes
    setup = await a_mentor_on(db_engine, "mp-window", "daily")

    session = await book(api_client, setup)

    (call,) = rooms.calls
    starts_at = dt.datetime.fromisoformat(session["starts_at"])
    assert call["opens_at"] == starts_at - dt.timedelta(minutes=5)
    assert call["closes_at"] == starts_at + dt.timedelta(minutes=60)


async def test_a_custom_venue_creates_nothing_and_keeps_the_mentors_url(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fakes: tuple[FakeRooms, FakeCalendar]
) -> None:
    """Nothing mints it — the mentor typed it.

    This is also the venue the model warns about: one static room for every
    session, so back-to-back sessions share it and an early joiner walks into
    the previous one.
    """
    rooms, calendar = fakes
    setup = await a_mentor_on(db_engine, "mp-custom", "custom")

    session = await book(api_client, setup)

    assert rooms.calls == []
    assert calendar.calls[0]["wants_conference"] is False
    assert (await venue_of(db_engine, session["id"]))["meeting_url"] == CUSTOM_URL


async def test_a_mentor_with_no_option_falls_back_to_meet(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fakes: tuple[FakeRooms, FakeCalendar]
) -> None:
    """The third step of the resolution, and not padding: every mentor is seeded
    a default, which makes the fallback look unreachable — and that is precisely
    the reasoning that failed for `primary_session_type_id`."""
    _, calendar = fakes
    setup = await a_mentor_on(db_engine, "mp-none", None)

    session = await book(api_client, setup)

    assert calendar.calls[0]["wants_conference"] is True
    assert (await venue_of(db_engine, session["id"]))["meeting_provider"] == "google_meet"


async def test_the_offerings_own_choice_beats_the_mentors_default(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fakes: tuple[FakeRooms, FakeCalendar]
) -> None:
    """**Provisioning must agree with what the mentee was shown.**

    The read models resolve the same way and share the `COALESCE`, so an
    offering listed as held on Daily that mints a Meet link would be a contract
    broken silently.
    """
    rooms, calendar = fakes
    setup = await a_mentor_on(db_engine, "mp-override", "google_meet")
    async with db_engine.begin() as conn:
        chosen = (
            await conn.execute(
                text(
                    "INSERT INTO mentor_conferencing_options (user_id, provider, is_default) "
                    "VALUES (:u, 'daily', false) RETURNING id"
                ),
                {"u": setup["mentor"]},
            )
        ).scalar_one()
        await conn.execute(
            text("UPDATE session_types SET conferencing_option_id = :o WHERE id = :t"),
            {"o": chosen, "t": setup["session_type"]},
        )

    await book(api_client, setup)

    assert len(rooms.calls) == 1
    assert calendar.calls[0]["wants_conference"] is False


# --------------------------------------------------------------------------
# When it happens
# --------------------------------------------------------------------------


async def test_a_request_gets_nothing_until_the_mentor_accepts(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fakes: tuple[FakeRooms, FakeCalendar]
) -> None:
    """**Minting a room for a request that may be declined leaves a room nobody
    uses** — and on a metered provider, one somebody pays for."""
    rooms, _ = fakes
    setup = await a_mentor_on(db_engine, "mp-pending", "daily", confirmation=True)

    session = await book(api_client, setup)

    assert session["status"] == "pending_mentor_approval"
    assert rooms.calls == []
    assert (await venue_of(db_engine, session["id"]))["meeting_url"] is None


async def test_accepting_is_the_second_confirmation_point(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fakes: tuple[FakeRooms, FakeCalendar]
) -> None:
    """The model has always said the link is generated per session at
    confirmation, and confirmation happens in two places — booking for an
    auto-confirming offering, and here for one that waits."""
    rooms, _ = fakes
    setup = await a_mentor_on(db_engine, "mp-accept", "daily", confirmation=True)
    session = await book(api_client, setup)

    accepted = await api_client.post(
        f"/api/v1/sessions/{session['id']}/accept", headers=setup["mentor_headers"]
    )

    assert accepted.status_code == 200, accepted.text
    assert len(rooms.calls) == 1
    assert (await venue_of(db_engine, session["id"]))["meeting_url"] is not None


async def test_declining_mints_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fakes: tuple[FakeRooms, FakeCalendar]
) -> None:
    """Only accepting produces `confirmed`. Declining, withdrawing and
    cancelling all end a session rather than starting one."""
    rooms, calendar = fakes
    setup = await a_mentor_on(db_engine, "mp-decline", "daily", confirmation=True)
    session = await book(api_client, setup)

    await api_client.post(
        f"/api/v1/sessions/{session['id']}/decline", headers=setup["mentor_headers"]
    )

    assert rooms.calls == []
    assert calendar.calls == []


async def test_an_unwired_provider_does_not_fail_the_booking(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The default state of the system, asserted rather than assumed.**

    No adapter is wired here, so the room provider raises and the calendar
    returns nothing. The session must still exist and hold its slot: a link can
    be minted later, where a booking refused because a third party was slow
    loses something that cannot be recovered.
    """
    setup = await a_mentor_on(db_engine, "mp-unwired", "daily")

    session = await book(api_client, setup)

    stored = await venue_of(db_engine, session["id"])
    assert stored["meeting_url"] is None
    assert stored["meeting_provider"] == "daily"


# --------------------------------------------------------------------------
# The door
# --------------------------------------------------------------------------


@dataclass
class FakeDoor(FakeRooms):
    """A room provider that also mints tokens, recording what it was asked."""

    tokens: list[dict[str, Any]] = field(default_factory=list)

    def token_for(self, **kwargs: Any) -> str:
        self.tokens.append(kwargs)
        return "minted-token"


@pytest.fixture
def door(api_client: httpx.AsyncClient) -> FakeDoor:
    rooms = FakeDoor()
    app = api_client._transport.app  # type: ignore[attr-defined]
    app.state.meeting_rooms = rooms
    app.state.calendar = FakeCalendar()
    return rooms


async def joinable(
    engine: AsyncEngine, client: httpx.AsyncClient, setup: dict[str, Any]
) -> dict[str, Any]:
    """A booked session moved into its own join window."""
    session = await book(client, setup)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET starts_at = now() + interval '1 minute' WHERE id = :i"),
            {"i": session["id"]},
        )
    return session


async def test_joining_a_daily_session_returns_a_tokenised_url(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, door: FakeDoor
) -> None:
    """**The gap this closes.** A private room refuses anybody without a token,
    so recording an arrival and handing back the stored address would send the
    participant somewhere that turns them away — worse than no link, because it
    looks like the platform is broken rather than unfinished."""
    setup = await a_mentor_on(db_engine, "door-daily", "daily")
    session = await joinable(db_engine, api_client, setup)

    joined = await api_client.post(
        f"/api/v1/sessions/{session['id']}/join", headers=setup["mentee_headers"]
    )

    assert joined.status_code == 200, joined.text
    assert joined.json()["meeting_url"] == "https://ef.daily.co/room?t=minted-token"
    # One token, minted at the moment somebody asked — not stored on the row.
    assert len(door.tokens) == 1


async def test_the_token_carries_the_caller_and_their_role(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, door: FakeDoor
) -> None:
    """Each party gets their own credential. The mentor is the owner — on Daily
    that is who may admit, mute and end — and handing both the same token would
    let a mentee end their mentor's session."""
    setup = await a_mentor_on(db_engine, "door-role", "daily")
    session = await joinable(db_engine, api_client, setup)

    await api_client.post(f"/api/v1/sessions/{session['id']}/join", headers=setup["mentee_headers"])
    await api_client.post(f"/api/v1/sessions/{session['id']}/join", headers=setup["mentor_headers"])

    mentee_token, mentor_token = door.tokens
    assert mentee_token["is_owner"] is False
    assert mentor_token["is_owner"] is True
    assert mentee_token["user_id"] != mentor_token["user_id"]


async def test_the_token_outlives_the_join_window(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, door: FakeDoor
) -> None:
    """One expiring when the window shuts would evict its holder fifteen minutes
    into an hour-long session — the same reason the room's own `exp` comes from
    the duration."""
    setup = await a_mentor_on(db_engine, "door-exp", "daily")
    session = await joinable(db_engine, api_client, setup)

    await api_client.post(f"/api/v1/sessions/{session['id']}/join", headers=setup["mentee_headers"])

    (minted,) = door.tokens
    assert minted["closes_at"] - minted["opens_at"] > dt.timedelta(minutes=60)


async def test_a_meet_session_returns_the_stored_link_unchanged(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, door: FakeDoor
) -> None:
    """Meet's link is on the calendar event and is not ours to gate. Minting a
    Daily token for it would produce a URL that goes nowhere."""
    setup = await a_mentor_on(db_engine, "door-meet", "google_meet")
    session = await joinable(db_engine, api_client, setup)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET meeting_url = :u WHERE id = :i"),
            {"u": "https://meet.google.com/abc", "i": session["id"]},
        )

    joined = await api_client.post(
        f"/api/v1/sessions/{session['id']}/join", headers=setup["mentee_headers"]
    )

    assert joined.json()["meeting_url"] == "https://meet.google.com/abc"
    assert door.tokens == []


async def test_an_unreachable_provider_still_records_the_arrival(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**A null door is not a refused join.** The arrival is committed either
    way, and a 500 here would fail a request that had already done the thing it
    was asked to do — for a session the participant is trying to attend *right
    now*."""
    setup = await a_mentor_on(db_engine, "door-down", "daily")
    session = await joinable(db_engine, api_client, setup)

    joined = await api_client.post(
        f"/api/v1/sessions/{session['id']}/join", headers=setup["mentee_headers"]
    )

    assert joined.status_code == 200, joined.text
    assert joined.json()["meeting_url"] is None
    async with db_engine.connect() as conn:
        status = (
            await conn.execute(
                text(
                    "SELECT attendance_status FROM session_participants "
                    "WHERE session_id = :i AND role = 'mentee'"
                ),
                {"i": session["id"]},
            )
        ).scalar_one()
    assert status == "attended"


async def test_a_refused_join_gets_no_door(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, door: FakeDoor
) -> None:
    """Outside the window there is nothing to open, and minting a token anyway
    would hand out a working credential for a session nobody may join yet."""
    setup = await a_mentor_on(db_engine, "door-early", "daily")
    session = await book(api_client, setup)

    refused = await api_client.post(
        f"/api/v1/sessions/{session['id']}/join", headers=setup["mentee_headers"]
    )

    assert refused.status_code == 409, refused.text
    assert door.tokens == []
