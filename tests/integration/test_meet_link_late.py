"""A Meet session's link is added late, not at booking (#384).

The calendar event is made with no conference, so for most of a session's life
the only way in is the session page, where pressing Join is recorded. The Meet
is patched onto the event at the last reminder; and because the join window
opens before that reminder, the first press of Join adds it too, which also
covers a reminder that failed or never ran.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.meeting_fakes import FAKE_MEET, FakeCalendar
from tests.integration.test_api_meeting_provisioning import (
    a_mentor_on,
    book,
    joinable,
    venue_of,
)
from tests.integration.test_api_reminder_callback import PATH, believing_client, signed_headers

from app.domain.notifications import LAST_REMINDER_KIND, SESSION_REMINDERS

# `door` wires a fake room provider and calendar onto the app for every test.
pytestmark = [pytest.mark.db, pytest.mark.asyncio, pytest.mark.usefixtures("door")]


async def remind(
    engine: AsyncEngine, calendar: FakeCalendar, session_id: str, kind: str
) -> httpx.Response:
    """Fire one reminder through the real, signed callback."""
    client = believing_client(engine)
    client._transport.app.state.calendar = calendar  # type: ignore[attr-defined]
    body = json.dumps({"session_id": session_id, "kind": kind}).encode()
    async with client:
        return await client.post(PATH, content=body, headers=signed_headers(body))


async def a_meet_session(
    engine: AsyncEngine, client: httpx.AsyncClient, tag: str, provider: str = "google_meet"
) -> tuple[dict[str, Any], dict[str, Any]]:
    setup = await a_mentor_on(engine, tag, provider)
    return setup, await book(client, setup)


async def test_the_last_reminder_adds_the_meet(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Patched onto the event the guests were invited to**, with the session's
    own id as the request id, so a repeat is the same Meet rather than a second."""
    _, session = await a_meet_session(db_engine, api_client, "late-meet")
    calendar = FakeCalendar()

    answered = await remind(db_engine, calendar, session["id"], LAST_REMINDER_KIND)

    assert answered.status_code == 200, answered.text
    assert calendar.conferences == [{"external_id": "event-1", "request_id": session["id"]}]
    assert (await venue_of(db_engine, session["id"]))["meeting_url"] == FAKE_MEET


@pytest.mark.parametrize(
    "kind", [r.kind for r in SESSION_REMINDERS if r.kind != LAST_REMINDER_KIND]
)
async def test_an_earlier_reminder_adds_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, kind: str
) -> None:
    _, session = await a_meet_session(db_engine, api_client, f"late-early-{kind}")
    calendar = FakeCalendar()

    await remind(db_engine, calendar, session["id"], kind)

    assert calendar.conferences == []
    assert (await venue_of(db_engine, session["id"]))["meeting_url"] is None


@pytest.mark.parametrize("provider", ["daily", "custom"])
async def test_only_a_meet_session_gets_one(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, provider: str
) -> None:
    """**A second link on a Daily or custom event** is the failure with no error:
    the guest clicks whichever their calendar renders first. Even with no link
    of its own, as when the Daily room could not be made, the event exists and
    an empty column must not read as "a Meet session waiting for its link"."""
    _, session = await a_meet_session(db_engine, api_client, f"late-{provider}", provider)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET meeting_url = NULL WHERE id = :i"), {"i": session["id"]}
        )
    calendar = FakeCalendar()

    await remind(db_engine, calendar, session["id"], LAST_REMINDER_KIND)

    assert calendar.conferences == []
    assert (await venue_of(db_engine, session["id"]))["meeting_url"] is None


async def test_a_session_with_its_link_is_left_alone(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Sessions booked before this change carry the link already."""
    _, session = await a_meet_session(db_engine, api_client, "late-has")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET meeting_url = 'https://meet.google.com/old' WHERE id = :i"),
            {"i": session["id"]},
        )
    calendar = FakeCalendar()

    await remind(db_engine, calendar, session["id"], LAST_REMINDER_KIND)

    assert calendar.conferences == []


@pytest.mark.parametrize("status", ["completed", "no_show"])
async def test_a_late_reminder_adds_no_meet_to_a_settled_session(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, status: str
) -> None:
    """**A reminder QStash delivers late** (Codex on #402): the session was
    settled meanwhile, so its outcome is decided and the reminder is not sent.
    A Meet made now would be a way in after the fact. Join and the door still
    use their own wider rule, for a party who pressed Join in time."""
    _, session = await a_meet_session(db_engine, api_client, f"late-settled-{status}")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET status = :s WHERE id = :i"),
            {"s": status, "i": session["id"]},
        )
    calendar = FakeCalendar()

    await remind(db_engine, calendar, session["id"], LAST_REMINDER_KIND)

    assert calendar.conferences == []


async def test_a_called_off_session_gets_no_meet(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Even with its event still there**, as when Google refused the removal
    (`release_meeting` keeps the id then): the status decides, not the event."""
    setup, session = await a_meet_session(db_engine, api_client, "late-off")
    cancelled = await api_client.post(
        f"/api/v1/sessions/{session['id']}/cancel", headers=setup["mentee_headers"]
    )
    assert cancelled.status_code == 200, cancelled.text
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET external_calendar_event_id = 'event-1' WHERE id = :i"),
            {"i": session["id"]},
        )
    calendar = FakeCalendar()

    await remind(db_engine, calendar, session["id"], LAST_REMINDER_KIND)

    assert calendar.conferences == []


async def test_a_session_with_no_event_asks_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """No event, nothing to patch: the calendar was unconfigured or refused."""
    _, session = await a_meet_session(db_engine, api_client, "late-noevt")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET external_calendar_event_id = NULL WHERE id = :i"),
            {"i": session["id"]},
        )
    calendar = FakeCalendar()

    await remind(db_engine, calendar, session["id"], LAST_REMINDER_KIND)

    assert calendar.conferences == []


async def test_a_refusal_still_sends_the_reminder(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The reminder is not hostage to Google.** It answers 200, its email is
    queued, and the session waits for Join to try again."""
    _, session = await a_meet_session(db_engine, api_client, "late-refused")
    calendar = FakeCalendar(refuses=True)

    answered = await remind(db_engine, calendar, session["id"], LAST_REMINDER_KIND)

    assert answered.status_code == 200, answered.text
    assert len(calendar.conferences) == 1
    assert (await venue_of(db_engine, session["id"]))["meeting_url"] is None
    async with db_engine.connect() as conn:
        queued = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM outbox_events "
                    "WHERE entity_id = :i AND payload->>'kind' = :k"
                ),
                {"i": session["id"], "k": LAST_REMINDER_KIND},
            )
        ).scalar_one()
    assert queued == 2


# --------------------------------------------------------------------------
# Join comes first: the window opens before the last reminder
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["join", "door"])
async def test_the_first_way_in_adds_the_meet(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, path: str
) -> None:
    """**Join opens ten minutes out, the last reminder fires at five**, so the
    party who presses Join first is the one who needs the link. Stored, so the
    other party is handed the same Meet."""
    setup = await a_mentor_on(db_engine, f"late-{path}", "google_meet")
    session = await joinable(db_engine, api_client, setup)
    calendar: FakeCalendar = api_client._transport.app.state.calendar  # type: ignore[attr-defined]

    entered = await api_client.post(
        f"/api/v1/sessions/{session['id']}/{path}", headers=setup["mentee_headers"]
    )
    again = await api_client.post(
        f"/api/v1/sessions/{session['id']}/{path}", headers=setup["mentor_headers"]
    )

    assert entered.status_code == 200, entered.text
    assert entered.json()["meeting_url"] == FAKE_MEET
    assert again.json()["meeting_url"] == FAKE_MEET
    assert len(calendar.conferences) == 1
    assert (await venue_of(db_engine, session["id"]))["meeting_url"] == FAKE_MEET


async def test_a_refused_meet_still_records_the_arrival(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**No link is a venue failure, not a refused Join**: the press is recorded
    and the door is null, as for any venue that cannot be reached."""
    setup = await a_mentor_on(db_engine, "late-join-refused", "google_meet")
    session = await joinable(db_engine, api_client, setup)
    api_client._transport.app.state.calendar = FakeCalendar(refuses=True)  # type: ignore[attr-defined]

    joined = await api_client.post(
        f"/api/v1/sessions/{session['id']}/join", headers=setup["mentee_headers"]
    )

    assert joined.status_code == 200, joined.text
    assert joined.json() == {"joined": True, "meeting_url": None}
    async with db_engine.connect() as conn:
        pressed = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM session_participants "
                    "WHERE session_id = :i AND joined_at IS NOT NULL"
                ),
                {"i": session["id"]},
            )
        ).scalar_one()
    assert pressed == 1


class LosesTheRace(FakeCalendar):
    """Another caller stores the Meet while this one's request is refused: the
    rate-limited second patch #402's spike measured."""

    def __init__(self, engine: AsyncEngine, session_id: str) -> None:
        super().__init__(refuses=True)
        self._engine, self._session_id = engine, session_id
        self._loop = asyncio.get_running_loop()

    def add_conference(self, external_id: str, *, request_id: str) -> str:
        asyncio.run_coroutine_threadsafe(self._store_theirs(), self._loop).result()
        return super().add_conference(external_id, request_id=request_id)

    async def _store_theirs(self) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text("UPDATE sessions SET meeting_url = :u WHERE id = :i"),
                {"u": FAKE_MEET, "i": self._session_id},
            )


async def test_a_refused_patch_still_hands_over_the_meet_someone_else_stored(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Both parties pressing Join together**: one patch wins, the other is
    refused. The loser is handed the winner's link rather than nothing."""
    setup = await a_mentor_on(db_engine, "late-race", "google_meet")
    session = await joinable(db_engine, api_client, setup)
    api_client._transport.app.state.calendar = LosesTheRace(db_engine, session["id"])  # type: ignore[attr-defined]

    joined = await api_client.post(
        f"/api/v1/sessions/{session['id']}/join", headers=setup["mentee_headers"]
    )

    assert joined.status_code == 200, joined.text
    assert joined.json()["meeting_url"] == FAKE_MEET
