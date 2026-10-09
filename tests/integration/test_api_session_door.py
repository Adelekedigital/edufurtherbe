"""Getting into a session's room: re-entry through the door, short sessions,
what an absent link looks like, and who may still come in late (#379, #380, #382).

Split out of `test_api_meeting_provisioning.py` at the 900-code-line limit
(non-negotiable #11). The venue and room fixtures it shares stay there and are
imported from it, so there is one copy of each.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.meeting_fakes import FakeCalendar, FakeDoor
from tests.integration.test_api_attendance import settle
from tests.integration.test_api_meeting_provisioning import (
    CUSTOM_URL,
    a_mentor_on,
    move_start,
    pressed_in_time,
    seen_in_the_room,
    started,
    venue_of,
)

from app.infra.clients.meetings import MeetingRoom, VenueUnavailableError

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


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
    await pressed_in_time(db_engine, session["id"])

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
    # Ten minutes early (the default lead) to the end of an hour-long session.
    assert by_door["closes_at"] - by_door["opens_at"] == dt.timedelta(minutes=70)
    assert joined.json()["meeting_url"] == entered.json()["meeting_url"]


async def test_only_the_mentor_door_carries_owner_rights(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, door: FakeDoor
) -> None:
    """The refusing half of the owner rule: a mentee is never minted an owner
    token, which would let them end their mentor's session."""
    setup = await a_mentor_on(db_engine, "door-mentee-owner", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=20)
    await pressed_in_time(db_engine, session["id"])

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
    await pressed_in_time(db_engine, session["id"])

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
    await seen_in_the_room(db_engine, session["id"])
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
    api_client._transport.app.state.calendar = FakeCalendar()  # type: ignore[attr-defined]
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
    await seen_in_the_room(db_engine, session["id"])
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
    api_client._transport.app.state.calendar = FakeCalendar()  # type: ignore[attr-defined]
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
    await pressed_in_time(db_engine, session["id"])

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
    # The press is what is kept; for a Daily session attendance then waits for
    # Daily to see the party in the room (#382).
    assert mentee["attendance_status"] == "pending"


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
    await pressed_in_time(db_engine, session["id"])
    assert (await venue_of(db_engine, session["id"]))["external_room_id"] is None

    entered = await api_client.post(door_url(session), headers=setup["mentee_headers"])

    assert entered.status_code == 200, entered.text
    assert entered.json() == {"meeting_url": None}


# --------------------------------------------------------------------------
# No late first-timers (owner, 2026-10-08)
#
# After arrivals stop, the door admits only a party who pressed Join in time.
# The frontend already offers Rejoin only then; this closes the direct-API path.
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("door")
async def test_after_arrivals_stop_a_party_who_never_joined_gets_no_door(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    setup = await a_mentor_on(db_engine, "door-late-first", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=20)

    entered = await api_client.post(door_url(session), headers=setup["mentee_headers"])

    assert entered.status_code == 409, entered.text
    assert entered.json()["type"] == "/problems/join-window-closed"


@pytest.mark.usefixtures("door")
async def test_after_arrivals_stop_a_party_who_joined_in_time_still_gets_back_in(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The positive half: the press in time is the ticket back in."""
    setup = await a_mentor_on(db_engine, "door-late-return", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=1)
    pressed = await api_client.post(
        f"/api/v1/sessions/{session['id']}/join", headers=setup["mentee_headers"]
    )
    assert pressed.status_code == 200, pressed.text
    await move_start(db_engine, session["id"], 20)

    entered = await api_client.post(door_url(session), headers=setup["mentee_headers"])

    assert entered.status_code == 200, entered.text
    assert entered.json()["meeting_url"]


@pytest.mark.usefixtures("door")
async def test_before_arrivals_stop_the_door_needs_no_earlier_press(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Unchanged inside the window: the rule only bites once arrivals stop."""
    setup = await a_mentor_on(db_engine, "door-early-nopress", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=5)

    entered = await api_client.post(door_url(session), headers=setup["mentee_headers"])

    assert entered.status_code == 200, entered.text
