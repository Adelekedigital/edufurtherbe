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
from tests.integration.test_api_attendance import settle

from app.domain.messages import MessageContext, build_variables
from app.infra.clients.meetings import CalendarEvent, MeetingRoom, VenueUnavailableError
from conftest import api_token, bearer, fund_by_auth

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
        # Booking spends a credit from PR 6 onward; this mentee has to be
        # able to pay for the sessions the test makes.
        await fund_by_auth(conn, mentee_auth)
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


# --------------------------------------------------------------------------
# Who is invited, and where the invite points (#389)
#
# ADR 0012 decided "both parties, by invitation" and the adapter's docstring
# said so, while the call invited the mentee alone for seven weeks — every test
# here checked that an event was made, none checked who was on it. And the
# invite carried a Daily room's bare URL, which Daily refuses without a token.
# --------------------------------------------------------------------------

APP = "https://app.example.test"


def serving_the_app_at(api_client: httpx.AsyncClient, origin: str | None) -> None:
    """Run the app with `APP_BASE_URL` set, the one thing the link is built from."""
    app = api_client._transport.app  # type: ignore[attr-defined]
    app.state.settings = app.state.settings.model_copy(update={"app_base_url": origin})


async def emails_of(engine: AsyncEngine, session_id: str) -> tuple[str, str]:
    """The mentee's and the mentor's addresses, read from the database."""
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT mentee.email AS mentee, mentor.email AS mentor "
                        "FROM sessions s "
                        "JOIN users mentee ON mentee.id = s.mentee_id "
                        "JOIN users mentor ON mentor.id = s.mentor_id "
                        "WHERE s.id = :i"
                    ),
                    {"i": session_id},
                )
            )
            .mappings()
            .one()
        )
    return str(row["mentee"]), str(row["mentor"])


@pytest.mark.parametrize("provider", ["daily", "google_meet", "custom"])
async def test_both_parties_are_invited_whatever_the_venue(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    fakes: tuple[FakeRooms, FakeCalendar],
    provider: str,
) -> None:
    """**Exactly the mentee and the mentor**: a missing party fails, and so does
    an extra. On Meet, an uninvited mentor knocks on a call nobody present can
    admit them to (ADR 0012 §4)."""
    _, calendar = fakes
    calendar.hands_back_a_link = provider == "google_meet"
    setup = await a_mentor_on(db_engine, f"inv-both-{provider}", provider)

    session = await book(api_client, setup)

    (call,) = calendar.calls
    assert list(call["attendee_emails"]) == list(await emails_of(db_engine, session["id"]))


@pytest.mark.parametrize("provider", ["daily", "google_meet", "custom"])
async def test_every_invite_links_to_the_session_page_and_never_to_the_venue(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    fakes: tuple[FakeRooms, FakeCalendar],
    provider: str,
) -> None:
    """**The session page, for every venue** — the page the emails already link
    to, where Join is pressed and recorded. Never the room: a Daily room's URL
    is refused without a token, and a custom venue's would skip the press."""
    _, calendar = fakes
    calendar.hands_back_a_link = provider == "google_meet"
    serving_the_app_at(api_client, APP)
    setup = await a_mentor_on(db_engine, f"inv-link-{provider}", provider)

    session = await book(api_client, setup)

    (call,) = calendar.calls
    assert call["join_url"] == f"{APP}/sessions/{session['id']}"
    venue = (await venue_of(db_engine, session["id"]))["meeting_url"]
    assert venue is None or venue not in repr(call)


async def test_the_invite_and_the_emails_share_one_link(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    fakes: tuple[FakeRooms, FakeCalendar],
) -> None:
    """**One definition** (non-negotiable #8): the invite's link is what an email's
    `sessionUrl` resolves to, so the two cannot drift to different pages."""
    _, calendar = fakes
    serving_the_app_at(api_client, APP)
    setup = await a_mentor_on(db_engine, "inv-one-link", "daily")

    session = await book(api_client, setup)

    context = MessageContext(
        recipient_name="Ada",
        recipient_timezone="UTC",
        mentor_name="Mo",
        mentee_name="Ada",
        session_id=session["id"],
        app_base_url=APP,
    )
    emailed = build_variables(("sessionUrl",), context)
    assert calendar.calls[0]["join_url"] == emailed["sessionUrl"]


async def test_a_party_who_deleted_their_account_is_not_invited(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    fakes: tuple[FakeRooms, FakeCalendar],
) -> None:
    """**Soft-deleted rows are invisible** (AGENTS.md), and Codex caught this
    lookup missing the shared predicate. A mentee who deletes their account
    while a request waits must not have their retained address sent to Google
    when the mentor accepts. The mentor is still invited."""
    _, calendar = fakes
    setup = await a_mentor_on(db_engine, "inv-deleted", "daily", confirmation=True)
    session = await book(api_client, setup)
    mentee, mentor = await emails_of(db_engine, session["id"])
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET deleted_at = now() WHERE email = :e"), {"e": mentee}
        )

    accepted = await api_client.post(
        f"/api/v1/sessions/{session['id']}/accept", headers=setup["mentor_headers"]
    )

    assert accepted.status_code == 200, accepted.text
    (call,) = calendar.calls
    assert [email for email in call["attendee_emails"] if email] == [mentor]


async def test_with_no_app_origin_the_invite_still_goes_out_without_a_link(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    fakes: tuple[FakeRooms, FakeCalendar],
) -> None:
    """No `APP_BASE_URL`, no link to give — and no venue URL put there instead.
    The booking still succeeds and both parties still get the time."""
    _, calendar = fakes
    serving_the_app_at(api_client, None)
    setup = await a_mentor_on(db_engine, "inv-no-origin", "daily")

    await book(api_client, setup)

    (call,) = calendar.calls
    assert call["join_url"] is None
    assert len(call["attendee_emails"]) == 2


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


async def test_a_mentor_with_no_option_falls_back_to_edufurther_video(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fakes: tuple[FakeRooms, FakeCalendar]
) -> None:
    """The third step of the resolution, and not padding: every mentor is seeded
    a default, which makes the fallback look unreachable — and that is precisely
    the reasoning that failed for `primary_session_type_id`. The fallback is
    `daily` (owner, 2026-10-01): a room is made, and no Meet conference asked for."""
    rooms, calendar = fakes
    setup = await a_mentor_on(db_engine, "mp-none", None)

    session = await book(api_client, setup)

    assert len(rooms.calls) == 1
    assert calendar.calls[0]["wants_conference"] is False
    assert (await venue_of(db_engine, session["id"]))["meeting_provider"] == "daily"


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


async def move_start(engine: AsyncEngine, session_id: str, minutes_ago: int) -> None:
    """Put a session's start `minutes_ago` minutes in the past (negative: future).

    **The one way this file moves a session in time.** There were two — this
    and a `joinable` that wrote `now() + interval '1 minute'` — and a review
    pointed out they were the same rule twice (non-negotiable #8): the copy that
    later gains clock-skew handling is the one the other does not.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET starts_at = now() - make_interval(mins => :m) WHERE id = :i"),
            {"m": minutes_ago, "i": session_id},
        )


async def started(
    engine: AsyncEngine, client: httpx.AsyncClient, setup: dict[str, Any], minutes_ago: int
) -> dict[str, Any]:
    """A booked, confirmed session whose start was `minutes_ago` minutes ago."""
    session = await book(client, setup)
    await move_start(engine, session["id"], minutes_ago)
    return session


async def joinable(
    engine: AsyncEngine, client: httpx.AsyncClient, setup: dict[str, Any]
) -> dict[str, Any]:
    """A booked session starting a minute from now — inside its join window."""
    return await started(engine, client, setup, minutes_ago=-1)


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


# --------------------------------------------------------------------------
# The event a cancellation removes
# --------------------------------------------------------------------------


@dataclass
class TrackingCalendar(FakeCalendar):
    """A calendar that also remembers what it was asked to remove."""

    cancelled: list[str] = field(default_factory=list)

    def cancel_event(self, external_id: str) -> None:
        self.cancelled.append(external_id)


@pytest.fixture
def tracking(api_client: httpx.AsyncClient) -> TrackingCalendar:
    calendar = TrackingCalendar()
    app = api_client._transport.app  # type: ignore[attr-defined]
    app.state.meeting_rooms = FakeRooms()
    app.state.calendar = calendar
    return calendar


async def test_cancelling_removes_the_calendar_event(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, tracking: TrackingCalendar
) -> None:
    """**The partner fix, and it was missing.** `external_calendar_event_id` was
    written by provisioning and read by nobody — harmless while no event existed
    and a live defect the moment one does: a cancelled session would leave a
    meeting sitting in both parties' calendars forever."""
    setup = await a_mentor_on(db_engine, "rel-cancel", "daily")
    session = await book(api_client, setup)

    await api_client.post(
        f"/api/v1/sessions/{session['id']}/cancel", headers=setup["mentee_headers"]
    )

    assert tracking.cancelled == ["event-1"]
    assert (await venue_of(db_engine, session["id"]))["external_calendar_event_id"] is None


async def test_declining_removes_it_too(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, tracking: TrackingCalendar
) -> None:
    """Every transition that ends a session before it happens, not just cancel.
    A declined request whose event survives is the same stale invitation with a
    different name on it."""
    setup = await a_mentor_on(db_engine, "rel-decline", "daily", confirmation=True)
    session = await book(api_client, setup)
    await api_client.post(
        f"/api/v1/sessions/{session['id']}/accept", headers=setup["mentor_headers"]
    )

    await api_client.post(
        f"/api/v1/sessions/{session['id']}/cancel", headers=setup["mentor_headers"]
    )

    assert tracking.cancelled == ["event-1"]


async def test_accepting_creates_rather_than_removes(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, tracking: TrackingCalendar
) -> None:
    """`accept` is the one transition that provisions. Releasing there would
    delete the event it had just created."""
    setup = await a_mentor_on(db_engine, "rel-accept", "daily", confirmation=True)
    session = await book(api_client, setup)

    await api_client.post(
        f"/api/v1/sessions/{session['id']}/accept", headers=setup["mentor_headers"]
    )

    assert tracking.cancelled == []
    assert (await venue_of(db_engine, session["id"]))["external_calendar_event_id"] == "event-1"


async def test_a_session_with_no_event_asks_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Most sessions today, since no calendar is configured. Calling the
    provider with a null id would be a request that can only fail."""
    calendar = TrackingCalendar()
    app = api_client._transport.app  # type: ignore[attr-defined]
    app.state.meeting_rooms = FakeRooms()
    app.state.calendar = calendar
    setup = await a_mentor_on(db_engine, "rel-none", "daily")
    session = await book(api_client, setup)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET external_calendar_event_id = NULL WHERE id = :i"),
            {"i": session["id"]},
        )

    await api_client.post(
        f"/api/v1/sessions/{session['id']}/cancel", headers=setup["mentee_headers"]
    )

    assert calendar.cancelled == []


async def test_a_provider_that_refuses_leaves_the_id_in_place(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Cleared only on success.** A failed removal keeps the handle so a later
    run can try again — clearing it regardless would lose the only reference to
    an event still sitting in somebody's calendar.

    And the cancellation itself still succeeds: the session is off, and refusing
    to record that because Google was slow would leave the two facts
    disagreeing.
    """

    class Refusing(FakeCalendar):
        def cancel_event(self, external_id: str) -> None:
            del external_id
            raise VenueUnavailableError("google is down")

    app = api_client._transport.app  # type: ignore[attr-defined]
    app.state.meeting_rooms = FakeRooms()
    app.state.calendar = Refusing()
    setup = await a_mentor_on(db_engine, "rel-down", "daily")
    session = await book(api_client, setup)

    cancelled = await api_client.post(
        f"/api/v1/sessions/{session['id']}/cancel", headers=setup["mentee_headers"]
    )

    assert cancelled.status_code == 200, cancelled.text
    assert (await venue_of(db_engine, session["id"]))["external_calendar_event_id"] == "event-1"


# --------------------------------------------------------------------------
# Getting back in (#379)
#
# `/join` closes fifteen minutes after the start because that is when the
# outcome becomes decidable. The token it mints already lasts the whole
# session — but it is never stored, so a party who dropped at minute twenty, or
# simply refreshed the tab, held a credential valid until the end and could not
# be handed another. `/door` is that handle: same minting, longer window, and
# no attendance written.
#
# The offsets below sit minutes either side of boundaries fifteen and sixty
# minutes away. `starts_at` is moved with the database clock and the window is
# judged with the process clock — the two-clock mix recorded as a blind spot —
# but with margins this wide no plausible skew can move a result, which is the
# condition that blind spot names as safe.
# --------------------------------------------------------------------------


async def attendance_of(engine: AsyncEngine, session_id: str) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT role, joined_at, attendance_status FROM session_participants "
                "WHERE session_id = :i ORDER BY role"
            ),
            {"i": session_id},
        )
        return [dict(row) for row in result.mappings()]


def door_url(session: dict[str, Any]) -> str:
    return f"/api/v1/sessions/{session['id']}/door"


@pytest.mark.usefixtures("door")
async def test_a_party_who_dropped_can_get_back_in_after_the_join_window(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The defect #379 closes.** Twenty minutes into an hour, `/join` is shut
    and refuses — correctly, since an arrival now would change a decidable
    outcome — but the room is still open and the party still belongs in it.

    Watched to fail by judging the door against the join window instead: this
    then answers `409`, which is exactly where a mentee refreshing their tab
    used to land.
    """
    setup = await a_mentor_on(db_engine, "door-rejoin", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=20)

    joined = await api_client.post(
        f"/api/v1/sessions/{session['id']}/join", headers=setup["mentee_headers"]
    )
    entered = await api_client.post(door_url(session), headers=setup["mentee_headers"])

    assert joined.status_code == 409, "the arrival window should still be shut"
    assert entered.status_code == 200, entered.text
    assert entered.json() == {"meeting_url": "https://ef.daily.co/room?t=minted-token"}


@pytest.mark.usefixtures("door")
async def test_the_door_records_no_arrival(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Even inside the join window**, which is the strongest place to check it.
    A door that also marked you present would let a party be settled as attended
    without ever pressing Join, and pressing Join is the only signal of arrival
    this service has. So the response says nothing about joining, and the rows
    say nothing either.

    Watched to fail by having the door call `record_arrival` **and commit**, as
    `/join` does. Calling it without committing is *not* enough to fail this:
    `session_door` rolls its transaction back before calling Daily, which undoes
    the write. So "the door records nothing" rests on two layers — `door_row`
    writes nothing, and the release would discard a write that crept in — and a
    regression has to defeat both. A mutation batch found that out by surviving.
    """
    setup = await a_mentor_on(db_engine, "door-no-arrival", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=1)

    entered = await api_client.post(door_url(session), headers=setup["mentee_headers"])

    assert entered.status_code == 200, entered.text
    assert "joined" not in entered.json()
    rows = await attendance_of(db_engine, session["id"])
    assert [(r["role"], r["joined_at"], r["attendance_status"]) for r in rows] == [
        ("mentee", None, "pending"),
        ("mentor", None, "pending"),
    ]


@pytest.mark.usefixtures("door")
async def test_the_door_does_not_move_an_arrival_already_recorded(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`joined_at` is the *first* arrival. Re-entering through the door after a
    drop must not rewrite it to the re-entry, or "Joined at 6:01 pm" becomes the
    moment a wifi connection came back."""
    setup = await a_mentor_on(db_engine, "door-keeps-arrival", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=1)
    await api_client.post(f"/api/v1/sessions/{session['id']}/join", headers=setup["mentee_headers"])
    before = await attendance_of(db_engine, session["id"])

    await api_client.post(door_url(session), headers=setup["mentee_headers"])

    assert await attendance_of(db_engine, session["id"]) == before


async def test_the_door_closes_when_the_session_ends(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, door: FakeDoor
) -> None:
    """Seventy minutes into a sixty-minute session there is no room to enter.
    The accepting case is the twenty-minute test above; together they pin both
    ends of the stretch the join window used to cut short."""
    setup = await a_mentor_on(db_engine, "door-ended", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=70)

    entered = await api_client.post(door_url(session), headers=setup["mentee_headers"])

    assert entered.status_code == 409, entered.text
    assert door.tokens == []


async def test_the_door_does_not_open_before_the_join_window(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, door: FakeDoor
) -> None:
    """Opens with the join window, not earlier: the token is not valid before
    it, so a door issued sooner would open onto nothing."""
    setup = await a_mentor_on(db_engine, "door-early", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=-30)

    entered = await api_client.post(door_url(session), headers=setup["mentee_headers"])

    assert entered.status_code == 409, entered.text
    assert door.tokens == []


async def test_a_stranger_gets_no_door(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, door: FakeDoor
) -> None:
    """**404, indistinguishable from a session that does not exist** — scoped by
    `is_a_party` in the query, so a stranger matches no row rather than one that
    is refused. A token here would be a credential into somebody else's room."""
    setup = await a_mentor_on(db_engine, "door-owner", "daily")
    other = await a_mentor_on(db_engine, "door-stranger", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=20)

    entered = await api_client.post(door_url(session), headers=other["mentee_headers"])

    assert entered.status_code == 404, entered.text
    assert door.tokens == []


async def test_the_door_mints_the_same_credential_the_join_does(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, door: FakeDoor
) -> None:
    """**One minting for both paths**, checked by minting through both.

    The first version of this test called `/door` alone and compared its tokens
    with hard-coded values, so it could not notice the thing its name claims: if
    `/join` stopped using the shared minting and grew its own expiry or owner
    rule, it would still have passed. A review caught that. Now the same party
    mints once through each path, and the two requests must be identical.

    Also pinned: the mentor keeps owner rights — losing them would leave a
    session nobody can end — and the token runs from the join window opening to
    the session's end, sixty-five minutes for an hour.

    Watched to fail by passing `is_owner=False` in the shared minting, which
    breaks both paths at once, and by giving `/join` its own `closes_at`, which
    breaks only the comparison.
    """
    setup = await a_mentor_on(db_engine, "door-same-mint", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=1)

    joined = await api_client.post(
        f"/api/v1/sessions/{session['id']}/join", headers=setup["mentor_headers"]
    )
    entered = await api_client.post(door_url(session), headers=setup["mentor_headers"])

    assert (joined.status_code, entered.status_code) == (200, 200)
    by_join, by_door = door.tokens
    assert by_join == by_door
    assert by_door["is_owner"] is True
    assert by_door["closes_at"] - by_door["opens_at"] == dt.timedelta(minutes=65)
    assert joined.json()["meeting_url"] == entered.json()["meeting_url"]


async def test_only_the_mentor_door_carries_owner_rights(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, door: FakeDoor
) -> None:
    """The refusing half of the owner rule: a mentee is never minted an owner
    token, which would let them end their mentor's session."""
    setup = await a_mentor_on(db_engine, "door-mentee-owner", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=20)

    await api_client.post(door_url(session), headers=setup["mentee_headers"])

    (mentee_token,) = door.tokens
    assert mentee_token["is_owner"] is False


async def test_a_custom_venue_door_is_the_stored_address(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, door: FakeDoor
) -> None:
    """Only a private room needs a token. A mentor's own link is already a way
    in, so the door hands it back unchanged and mints nothing."""
    setup = await a_mentor_on(db_engine, "door-custom", "custom")
    session = await started(db_engine, api_client, setup, minutes_ago=20)

    entered = await api_client.post(door_url(session), headers=setup["mentee_headers"])

    assert entered.status_code == 200, entered.text
    assert entered.json() == {"meeting_url": CUSTOM_URL}
    assert door.tokens == []


async def status_of(engine: AsyncEngine, session_id: str) -> str:
    async with engine.connect() as conn:
        return str(
            (
                await conn.execute(
                    text("SELECT status FROM sessions WHERE id = :i"), {"i": session_id}
                )
            ).scalar_one()
        )


@pytest.mark.usefixtures("door")
async def test_the_door_still_opens_after_the_session_is_settled(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The case the door exists for, after the job that runs every hour.**

    Settlement moves a session out of `confirmed` the moment its join window
    shuts, fifteen minutes in — but the session is still running, and the room
    still open. A door that required `confirmed` stopped working at whichever
    point in the hour the job happened to run: for a 14:00 session, from 14:30.

    Both joined, so this one settles `completed`. Found by review; the earlier
    door tests never ran a settlement, so all of them passed.
    """
    setup = await a_mentor_on(db_engine, "door-after-settle", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=1)
    for side in ("mentee_headers", "mentor_headers"):
        joined = await api_client.post(
            f"/api/v1/sessions/{session['id']}/join", headers=setup[side]
        )
        assert joined.status_code == 200, joined.text
    await move_start(db_engine, session["id"], minutes_ago=20)
    await settle(db_engine)
    assert await status_of(db_engine, session["id"]) == "completed"

    entered = await api_client.post(door_url(session), headers=setup["mentee_headers"])

    assert entered.status_code == 200, entered.text
    assert entered.json()["meeting_url"].endswith("?t=minted-token")


@pytest.mark.usefixtures("door")
async def test_the_door_opens_for_a_session_settled_as_missed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`no_show` decides the outcome, not whether the room exists.

    The mentee arrived and the mentor did not, so the session settles as missed.
    The mentee is still sitting in an open room and may still drop out of it;
    their attendance is already recorded, and the door changes none of it.
    """
    setup = await a_mentor_on(db_engine, "door-after-noshow", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=1)
    await api_client.post(f"/api/v1/sessions/{session['id']}/join", headers=setup["mentee_headers"])
    await move_start(db_engine, session["id"], minutes_ago=20)
    await settle(db_engine)
    assert await status_of(db_engine, session["id"]) == "no_show"

    entered = await api_client.post(door_url(session), headers=setup["mentee_headers"])

    assert entered.status_code == 200, entered.text


@pytest.mark.usefixtures("door")
async def test_a_cancelled_session_has_no_door_even_inside_its_hour(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The refusing case for the widened rule.** A settled session was agreed
    to and happened, or was meant to; a cancelled one was called off, and its
    room should not open for anybody however recent the start."""
    api_client._transport.app.state.calendar = TrackingCalendar()  # type: ignore[attr-defined]
    setup = await a_mentor_on(db_engine, "door-cancelled", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=-30)
    cancelled = await api_client.post(
        f"/api/v1/sessions/{session['id']}/cancel", headers=setup["mentee_headers"]
    )
    assert cancelled.status_code == 200, cancelled.text
    await move_start(db_engine, session["id"], minutes_ago=20)

    entered = await api_client.post(door_url(session), headers=setup["mentee_headers"])

    assert entered.status_code == 409, entered.text


# --------------------------------------------------------------------------
# `door_closes_at` on the session read
#
# Published so the client never adds the duration to `starts_at` itself — the
# same reason `join_opens_at` and `join_closes_at` are. Null means there is no
# door at all, which is not the same as a door that has closed.
# --------------------------------------------------------------------------


async def read_session(
    client: httpx.AsyncClient, session: dict[str, Any], headers: dict[str, str]
) -> dict[str, Any]:
    shown = await client.get(f"/api/v1/sessions/{session['id']}", headers=headers)
    assert shown.status_code == 200, shown.text
    return dict(shown.json())


@pytest.mark.usefixtures("door")
async def test_a_running_session_publishes_when_its_door_closes(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The session's end: `starts_at` plus the duration, later than
    `join_closes_at` by exactly the stretch the door exists for."""
    setup = await a_mentor_on(db_engine, "door-closes-shown", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=20)

    shown = await read_session(api_client, session, setup["mentee_headers"])

    starts = dt.datetime.fromisoformat(shown["starts_at"])
    closes = dt.datetime.fromisoformat(shown["door_closes_at"])
    joining_ends = dt.datetime.fromisoformat(shown["join_closes_at"])
    assert closes - starts == dt.timedelta(minutes=shown["duration_minutes"])
    assert closes > joining_ends


@pytest.mark.usefixtures("door")
async def test_a_settled_session_still_publishes_its_door(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Not null after settlement**, which is the field-level twin of the bug a
    review caught in the endpoint. A client keying Rejoin off this would
    otherwise hide it at the exact moment the session settled."""
    setup = await a_mentor_on(db_engine, "door-closes-settled", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=1)
    for side in ("mentee_headers", "mentor_headers"):
        await api_client.post(f"/api/v1/sessions/{session['id']}/join", headers=setup[side])
    await move_start(db_engine, session["id"], minutes_ago=20)
    await settle(db_engine)

    shown = await read_session(api_client, session, setup["mentee_headers"])

    assert shown["status"] == "completed"
    assert shown["door_closes_at"] is not None


async def test_a_cancelled_session_publishes_no_door(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Null, not a time**, because a cancelled session has no room to enter —
    and *"you can get back in until 15:00"* on a called-off session would be
    false rather than merely stale. `join_closes_at` stays as it always has;
    only the door, which is a promise of entry, goes null."""
    api_client._transport.app.state.calendar = TrackingCalendar()  # type: ignore[attr-defined]
    api_client._transport.app.state.meeting_rooms = FakeDoor()  # type: ignore[attr-defined]
    setup = await a_mentor_on(db_engine, "door-closes-cancelled", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=-30)
    await api_client.post(
        f"/api/v1/sessions/{session['id']}/cancel", headers=setup["mentee_headers"]
    )

    shown = await read_session(api_client, session, setup["mentee_headers"])

    assert shown["status"] == "cancelled"
    assert shown["door_closes_at"] is None


# --------------------------------------------------------------------------
# Short sessions
#
# `SESSION_DURATION_MINUTES` permits five to four hundred and eighty minutes, so
# a session can end before the fifteen-minute arrival edge. #380 found the door
# closing first in that case and documented it. The owner then closed the cause
# (2026-10-08): **arrivals stop when the session ends**, so a party is never
# marked present at a session that is over, or handed a token for a closed room.
# The door and the arrival window now close together on a short session, and the
# door outlasts it on a long one — never the other way round.
# --------------------------------------------------------------------------


async def shortened(engine: AsyncEngine, session_id: str, minutes: int) -> None:
    """Make a booked session `minutes` long — a duration the product permits."""
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET duration_minutes = :d WHERE id = :i"),
            {"d": minutes, "i": session_id},
        )


@pytest.mark.usefixtures("door")
async def test_a_short_session_stops_taking_arrivals_when_it_ends(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`join_closes_at` is the session's end, not fifteen minutes in, and the
    door closes at the same instant — so the door never closes first."""
    setup = await a_mentor_on(db_engine, "join-short-closes", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=2)
    await shortened(db_engine, session["id"], 10)

    shown = await read_session(api_client, session, setup["mentee_headers"])

    starts_at = dt.datetime.fromisoformat(shown["starts_at"])
    ends = starts_at + dt.timedelta(minutes=10)
    assert dt.datetime.fromisoformat(shown["join_closes_at"]) == ends
    assert dt.datetime.fromisoformat(shown["door_closes_at"]) == ends


@pytest.mark.usefixtures("door")
async def test_a_join_after_a_short_session_ends_is_refused_and_records_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, door: FakeDoor
) -> None:
    """**The bug option A closes.** Twelve minutes into a ten-minute session the
    arrival was recorded and a token minted for a room that had already shut.
    Now it is a `409`, the mentee's row stays `pending`, and Daily is not asked."""
    setup = await a_mentor_on(db_engine, "join-short-ended", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=12)
    await shortened(db_engine, session["id"], 10)

    joined = await api_client.post(
        f"/api/v1/sessions/{session['id']}/join", headers=setup["mentee_headers"]
    )

    assert joined.status_code == 409, joined.text
    mentee = next(r for r in await attendance_of(db_engine, session["id"]) if r["role"] == "mentee")
    assert mentee["attendance_status"] == "pending"
    assert door.tokens == []


@pytest.mark.usefixtures("door")
async def test_a_join_just_before_a_short_session_ends_still_counts(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The positive half: nine minutes into a ten-minute session is still on time."""
    setup = await a_mentor_on(db_engine, "join-short-late", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=9)
    await shortened(db_engine, session["id"], 10)

    joined = await api_client.post(
        f"/api/v1/sessions/{session['id']}/join", headers=setup["mentee_headers"]
    )

    assert joined.status_code == 200, joined.text
    assert joined.json()["joined"] is True


@pytest.mark.usefixtures("door")
async def test_a_short_session_settles_at_its_end_not_fifteen_minutes_in(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The settlement's SQL follows the same rule as `/join`.** If it still
    waited fifteen minutes, a short session would sit undecided after arrivals
    had stopped. If it ran sooner than `/join` closed, it could brand somebody
    absent while they could still arrive. Both directions are checked: unsettled
    a minute before the end, settled a minute after."""
    early_setup = await a_mentor_on(db_engine, "settle-short-early", "daily")
    early = await started(db_engine, api_client, early_setup, minutes_ago=9)
    await shortened(db_engine, early["id"], 10)
    await settle(db_engine)
    assert await status_of(db_engine, early["id"]) == "confirmed"

    # A second mentor: one mentee may hold one live booking per mentor.
    late_setup = await a_mentor_on(db_engine, "settle-short-late", "daily")
    late = await started(db_engine, api_client, late_setup, minutes_ago=11)
    await shortened(db_engine, late["id"], 10)
    await settle(db_engine)
    assert await status_of(db_engine, late["id"]) == "no_show"


# --------------------------------------------------------------------------
# When there is no way in (#380, CLAUDE.md rule 12)
#
# `_door_for` answers `null` rather than raising when Daily will not mint, and
# that `except` had never run under any test — on `/door` *or* `/join`, which
# share it. A fallback no test reaches has never been executed.
# --------------------------------------------------------------------------


@dataclass
class RefusingDoor(FakeDoor):
    """A room provider that makes rooms and then will not mint a token for them."""

    def token_for(self, **kwargs: Any) -> str:
        self.tokens.append(kwargs)
        raise VenueUnavailableError("daily refused the token")


@pytest.fixture
def refusing_door(api_client: httpx.AsyncClient) -> RefusingDoor:
    rooms = RefusingDoor()
    app = api_client._transport.app  # type: ignore[attr-defined]
    app.state.meeting_rooms = rooms
    app.state.calendar = FakeCalendar()
    return rooms


async def test_a_door_daily_will_not_mint_is_null_not_a_500(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, refusing_door: RefusingDoor
) -> None:
    """**`200` with `null`, not a `500`.** What is missing is a way in, which the
    lobby can say honestly; a `500` would look like the platform broke.

    Watched to fail by letting the exception propagate from the shared minting.
    """
    setup = await a_mentor_on(db_engine, "door-refused", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=20)

    entered = await api_client.post(door_url(session), headers=setup["mentee_headers"])

    assert entered.status_code == 200, entered.text
    assert entered.json() == {"meeting_url": None}
    # It did ask: the null is Daily's refusal, not a request never made.
    assert len(refusing_door.tokens) == 1


@pytest.mark.usefixtures("refusing_door")
async def test_a_join_daily_will_not_mint_still_records_the_arrival(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The arrival survives the provider failing**, which is why `/join`
    commits before it mints. A party who pressed Join and was told "no link"
    must not then be settled absent — the press is the signal, and it happened.

    The same shared branch as the door's, reached through the other caller.
    """
    setup = await a_mentor_on(db_engine, "join-refused", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=1)

    joined = await api_client.post(
        f"/api/v1/sessions/{session['id']}/join", headers=setup["mentee_headers"]
    )

    assert joined.status_code == 200, joined.text
    assert joined.json() == {"joined": True, "meeting_url": None}
    mentee = next(r for r in await attendance_of(db_engine, session["id"]) if r["role"] == "mentee")
    assert mentee["joined_at"] is not None
    assert mentee["attendance_status"] == "attended"


class NoRooms:
    """A room provider that cannot make rooms, so provisioning leaves none."""

    def create(self, **_kwargs: Any) -> MeetingRoom:
        raise VenueUnavailableError("daily is down")

    def token_for(self, **_kwargs: Any) -> str:  # pragma: no cover - no room to mint for
        raise AssertionError("a door with no room must not ask for a token")


async def test_a_session_whose_room_was_never_made_has_a_null_door(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The real "nowhere to meet" case.** Provisioning deliberately leaves the
    room null rather than failing the booking when the provider is down, so the
    session is real and running with no room behind it. The door says so with a
    `200` and `null` — and asks Daily for nothing, since there is nothing to mint.

    A first version of this test gave the mentor no venue and expected `null`.
    It failed, correctly: a mentor who never chose gets the platform default,
    which is Daily, so a room *was* made. The premise was wrong, not the code.
    """
    app = api_client._transport.app  # type: ignore[attr-defined]
    app.state.meeting_rooms = NoRooms()
    app.state.calendar = FakeCalendar()
    setup = await a_mentor_on(db_engine, "door-no-room", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=20)
    assert (await venue_of(db_engine, session["id"]))["external_room_id"] is None

    entered = await api_client.post(door_url(session), headers=setup["mentee_headers"])

    assert entered.status_code == 200, entered.text
    assert entered.json() == {"meeting_url": None}
