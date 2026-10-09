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
from tests.integration.meeting_fakes import FakeCalendar, FakeDoor, FakeRooms

from app.domain.messages import MessageContext, build_variables
from app.infra.clients.meetings import VenueUnavailableError
from conftest import api_token, bearer, fund_by_auth

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

CUSTOM_URL = "https://mentor.example.test/room"


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


async def test_the_room_opens_at_the_ceiling_and_the_token_at_the_setting(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, door: FakeDoor
) -> None:
    """**A config change must not strand provisioned rooms** (Codex on #391).

    A room's opening is fixed when it is created. If it carried the configured
    lead, raising the setting later would publish a Join button ahead of rooms
    already made, and Daily would refuse at the room. So every room opens at the
    ceiling, and the per-request token carries the configured lead. Daily
    enforces both, so the token decides, and a changed setting applies to every
    session at once. The room is private, so it admits nobody without a token.
    """
    app = api_client._transport.app  # type: ignore[attr-defined]
    app.state.settings = app.state.settings.model_copy(update={"join_window_opens_minutes": 3})
    setup = await a_mentor_on(db_engine, "lead-ceiling", "daily")
    # The room is made at booking, against the start as booked.
    session = await started(db_engine, api_client, setup, minutes_ago=-2)
    booked_start = dt.datetime.fromisoformat(session["starts_at"])
    # The token is minted at /join, against the start as it now stands.
    async with db_engine.connect() as conn:
        moved_start = (
            await conn.execute(
                text("SELECT starts_at FROM sessions WHERE id = :i"), {"i": session["id"]}
            )
        ).scalar_one()

    joined = await api_client.post(
        f"/api/v1/sessions/{session['id']}/join", headers=setup["mentee_headers"]
    )

    assert joined.status_code == 200, joined.text
    (room,) = door.calls
    assert room["opens_at"] == booked_start - dt.timedelta(minutes=10)
    assert door.tokens[-1]["opens_at"] == moved_start - dt.timedelta(minutes=3)


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
    # The room opens with the join window: ten minutes early by default (owner,
    # 2026-10-08), so nobody is handed a Join button onto a shut room.
    assert call["opens_at"] == starts_at - dt.timedelta(minutes=10)
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


async def pressed_in_time(engine: AsyncEngine, session_id: str) -> None:
    """Both parties pressed Join while arrivals were open, as a party who has
    since dropped would have. After arrivals stop the door admits only them."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE session_participants p SET joined_at = s.starts_at "
                "FROM sessions s WHERE s.id = p.session_id AND s.id = :i"
            ),
            {"i": session_id},
        )


async def seen_in_the_room(engine: AsyncEngine, session_id: str) -> None:
    """Daily saw both parties in the room in time (#382), so the session can
    settle `completed`; for a Daily session the press alone no longer counts."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE session_participants p SET in_room_at = s.starts_at, "
                "attendance_status = 'attended' "
                "FROM sessions s WHERE s.id = p.session_id AND s.id = :i"
            ),
            {"i": session_id},
        )


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
