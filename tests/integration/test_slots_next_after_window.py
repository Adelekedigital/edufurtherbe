"""The next free time past a full booking window (owner, 2026-11-21).

When nothing is free anywhere inside an offering's window, `/slots` says when
the next free time after it is, and when that time becomes bookable, so the
booking step can say "next time is Sun, Dec 6; you can book it from Sun, Nov
22". The window stays a hard limit: these times are never in `data`.
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.integration.factories import add_block, make_public_mentor
from tests.integration.test_booking_window_break import NOW, at, mentor_with_hours, offering

from app.core.errors import UpstreamError
from app.infra.db.slot_store import next_after_window
from conftest import PLATFORM_WINDOW

pytestmark = [pytest.mark.db, pytest.mark.anyio]

WEEK = 7


async def next_after(engine: AsyncEngine, mentor: object, session_type: object) -> object:
    async with AsyncSession(engine) as session:
        return await next_after_window(
            session,
            mentor,  # type: ignore[arg-type]
            session_type,  # type: ignore[arg-type]
            now=NOW,
            window=PLATFORM_WINDOW,
        )


async def test_a_full_window_names_the_next_time_after_it_and_when_it_opens(
    db_engine: AsyncEngine,
) -> None:
    """A seven-day window from Monday 7 Jan 00:00 with every day through the
    next Monday blocked: the next free start is Tuesday 15 Jan 09:00, bookable
    one window earlier, from Tuesday 8 Jan 09:00."""
    mentor = await mentor_with_hours(db_engine, "after-window")
    session_type = await offering(db_engine, mentor, "Week", window=WEEK)
    for day in range(WEEK + 1):
        await add_block(db_engine, mentor, NOW.date() + dt.timedelta(days=day))

    found = await next_after(db_engine, mentor, session_type)

    assert found == (at(8, 9), at(1, 9))


async def test_a_window_with_a_free_slot_names_nothing(db_engine: AsyncEngine) -> None:
    mentor = await mentor_with_hours(db_engine, "inside-window")
    session_type = await offering(db_engine, mentor, "Week", window=WEEK)
    for day in range(WEEK - 1):
        await add_block(db_engine, mentor, NOW.date() + dt.timedelta(days=day))

    assert await next_after(db_engine, mentor, session_type) is None


async def test_nothing_free_after_the_window_either_names_nothing(db_engine: AsyncEngine) -> None:
    mentor = await make_public_mentor(db_engine, "never-free", timezone="UTC")
    session_type = await offering(db_engine, mentor, "Week", window=WEEK)

    assert await next_after(db_engine, mentor, session_type) is None


async def test_the_offerings_own_window_decides_where_after_begins(
    db_engine: AsyncEngine,
) -> None:
    """A fortnight's offering with its first week blocked has a slot inside
    its window, so nothing is named, though a week's would have."""
    mentor = await mentor_with_hours(db_engine, "own-window")
    session_type = await offering(db_engine, mentor, "Fortnight", window=14)
    for day in range(WEEK + 1):
        await add_block(db_engine, mentor, NOW.date() + dt.timedelta(days=day))

    assert await next_after(db_engine, mentor, session_type) is None


async def test_an_unbookable_offering_names_nothing(db_engine: AsyncEngine) -> None:
    mentor = await mentor_with_hours(db_engine, "wrong-mentor")
    other = await mentor_with_hours(db_engine, "wrong-mentor-2")
    session_type = await offering(db_engine, other, "Theirs", window=WEEK)

    assert await next_after(db_engine, mentor, session_type) is None


async def test_slots_carries_both_fields_null_while_the_window_has_room(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await mentor_with_hours(db_engine, "api-room")
    session_type = await offering(db_engine, mentor, "Week", window=WEEK)

    response = await api_client.get(
        f"/api/v1/users/{mentor}/availability/slots?session_type_id={session_type}"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["data"]
    assert body["next_after_window_at"] is None
    assert body["bookable_from"] is None


async def test_slots_names_the_next_time_when_the_window_is_full(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await mentor_with_hours(db_engine, "api-full")
    session_type = await offering(db_engine, mentor, "Week", window=WEEK)
    today = dt.datetime.now(dt.UTC).date()
    for day in range(WEEK + 2):
        await add_block(db_engine, mentor, today + dt.timedelta(days=day))

    response = await api_client.get(
        f"/api/v1/users/{mentor}/availability/slots?session_type_id={session_type}"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["data"] == []
    after = dt.datetime.fromisoformat(body["next_after_window_at"])
    opens = dt.datetime.fromisoformat(body["bookable_from"])
    assert after == dt.datetime.combine(
        today + dt.timedelta(days=WEEK + 2), dt.time(9), tzinfo=dt.UTC
    )
    assert after - opens == dt.timedelta(days=WEEK)


class CountingCalendar:
    """A connected mentor's calendar with nothing busy, counting its reads."""

    def __init__(self, fail_past: dt.datetime | None = None) -> None:
        self.calls: list[tuple[dt.datetime, dt.datetime]] = []
        self.fail_past = fail_past

    async def busy(
        self, _session: object, _user_id: object, start: dt.datetime, end: dt.datetime
    ) -> tuple[()]:
        self.calls.append((start, end))
        if self.fail_past is not None and end > self.fail_past:
            raise UpstreamError("free/busy budget spent")
        return ()


async def test_both_lookups_share_one_calendar_read(db_engine: AsyncEngine) -> None:
    """Review of #719: an empty anonymous page drew three budgeted Google calls
    (decision 242). The window check and the look past it now share one."""
    mentor = await mentor_with_hours(db_engine, "one-read")
    session_type = await offering(db_engine, mentor, "Week", window=WEEK)
    for day in range(WEEK + 1):
        await add_block(db_engine, mentor, NOW.date() + dt.timedelta(days=day))
    calendar = CountingCalendar()

    async with AsyncSession(db_engine) as session:
        found = await next_after_window(
            session,
            mentor,
            session_type,
            now=NOW,
            window=PLATFORM_WINDOW,
            external_busy=calendar,
        )

    assert found == (at(8, 9), at(1, 9))
    assert len(calendar.calls) == 1


async def test_a_calendar_that_cannot_be_read_names_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A spent budget or a Google error must not name a time the calendar may
    have busy: the page still answers, with both fields null."""
    mentor = await mentor_with_hours(db_engine, "no-read")
    session_type = await offering(db_engine, mentor, "Week", window=WEEK)
    today = dt.datetime.now(dt.UTC).date()
    for day in range(WEEK + 2):
        await add_block(db_engine, mentor, today + dt.timedelta(days=day))
    app = api_client._transport.app  # type: ignore[attr-defined]
    app.state.free_busy = CountingCalendar(
        fail_past=dt.datetime.now(dt.UTC) + dt.timedelta(days=WEEK + 3)
    )

    response = await api_client.get(
        f"/api/v1/users/{mentor}/availability/slots?session_type_id={session_type}"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["data"] == []
    assert body["next_after_window_at"] is None and body["bookable_from"] is None
