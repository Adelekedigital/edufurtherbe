"""A calendar event an ending could not remove is retried by the hourly run (#423).

`release_meeting` keeps `external_calendar_event_id` when Google refuses, so
that a later run can try again. These pin the later run: the settlement job
retries ended sessions that still carry an event, soonest first and a bounded
number per run, and never touches a session that is still on.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.meeting_fakes import FakeCalendar, FakeRooms
from tests.integration.test_api_meeting_provisioning import a_mentor_on, book, venue_of
from tests.off_the_loop import guarded

from app.core.config import Settings
from app.infra.db.session_writer import retry_releases
from app.infra.jobs import runner
from app.infra.jobs.runner import RuntimeJobs
from conftest import A_REASON

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def event_of(engine: AsyncEngine, session_id: str) -> str | None:
    value = (await venue_of(engine, session_id))["external_calendar_event_id"]
    return None if value is None else str(value)


async def booked_with_event(
    api_client: httpx.AsyncClient, engine: AsyncEngine, tag: str, *, starts_in: dt.timedelta
) -> tuple[str, dict[str, Any]]:
    """A confirmed session with its own event id, starting `starts_in` from now."""
    app = api_client._transport.app  # type: ignore[attr-defined]
    app.state.meeting_rooms = FakeRooms()
    app.state.calendar = FakeCalendar(refuses_cancel=True)
    setup = await a_mentor_on(engine, tag, "daily")
    session = await book(api_client, setup)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE sessions SET external_calendar_event_id = :e, starts_at = :s WHERE id = :i"
            ),
            {"e": f"evt-{tag}", "s": dt.datetime.now(dt.UTC) + starts_in, "i": session["id"]},
        )
    return str(session["id"]), setup


async def cancelled_while_google_refused(
    api_client: httpx.AsyncClient,
    engine: AsyncEngine,
    tag: str,
    *,
    starts_in: dt.timedelta = dt.timedelta(days=2),
) -> str:
    """Cancelled through the real ending path while the calendar refused, so the
    event id is still on the row."""
    session_id, setup = await booked_with_event(api_client, engine, tag, starts_in=starts_in)
    cancelled = await api_client.post(
        f"/api/v1/sessions/{session_id}/cancel", headers=setup["mentee_headers"], json=A_REASON
    )
    assert cancelled.status_code == 200, cancelled.text
    assert await event_of(engine, session_id) == f"evt-{tag}"
    return session_id


async def retry(engine: AsyncEngine, calendar: FakeCalendar, **kwargs: Any) -> int:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        return await retry_releases(
            session,
            now=dt.datetime.now(dt.UTC),
            calendar=guarded("calendar", calendar),
            **kwargs,
        )


async def test_a_refused_cancel_is_retried_and_cleared(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    session_id = await cancelled_while_google_refused(api_client, db_engine, "rt-ok")
    calendar = FakeCalendar()

    released = await retry(db_engine, calendar)

    assert released == 1
    assert calendar.cancelled == ["evt-rt-ok"]
    assert await event_of(db_engine, session_id) is None


async def test_a_retry_refused_again_keeps_the_id_and_the_rest_still_run(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**One refusal does not stop the run.** The refused event keeps its handle
    for the next hour; the one after it is still removed."""
    stuck = await cancelled_while_google_refused(
        api_client, db_engine, "rt-stuck", starts_in=dt.timedelta(days=1)
    )
    later = await cancelled_while_google_refused(
        api_client, db_engine, "rt-later", starts_in=dt.timedelta(days=3)
    )
    calendar = FakeCalendar(refuses_cancel=frozenset({"evt-rt-stuck"}))

    released = await retry(db_engine, calendar)

    assert calendar.asked_to_cancel == ["evt-rt-stuck", "evt-rt-later"]
    assert released == 1
    assert await event_of(db_engine, stuck) == "evt-rt-stuck"
    assert await event_of(db_engine, later) is None


async def test_a_session_that_is_still_on_is_never_touched(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Only an ended session's event is removed: a confirmed one's is the
    invitation both parties are relying on."""
    live, _ = await booked_with_event(
        api_client, db_engine, "rt-live", starts_in=dt.timedelta(days=1)
    )
    ended = await cancelled_while_google_refused(api_client, db_engine, "rt-ended")
    calendar = FakeCalendar()

    released = await retry(db_engine, calendar)

    assert released == 1
    assert calendar.asked_to_cancel == ["evt-rt-ended"]
    assert await event_of(db_engine, live) == "evt-rt-live"
    assert await event_of(db_engine, ended) is None


async def test_an_event_whose_session_has_started_is_left(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Only a session still ahead.** Once its time has passed, nobody can turn
    up to it, and deleting the event would email both parties a cancellation of
    something already over — every migrated ended session with an event id
    would get one on the first run."""
    session_id = await cancelled_while_google_refused(api_client, db_engine, "rt-past")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET starts_at = now() - interval '1 minute' WHERE id = :i"),
            {"i": session_id},
        )
    calendar = FakeCalendar()

    released = await retry(db_engine, calendar)

    assert released == 0
    assert calendar.asked_to_cancel == []
    assert await event_of(db_engine, session_id) == "evt-rt-past"


async def test_a_run_retries_a_bounded_number_soonest_first(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Bounded, so a long backlog cannot run the job out of time**, and soonest
    first, so the invitation closest to misleading somebody goes first."""
    last = await cancelled_while_google_refused(
        api_client, db_engine, "rt-c", starts_in=dt.timedelta(days=3)
    )
    await cancelled_while_google_refused(
        api_client, db_engine, "rt-a", starts_in=dt.timedelta(days=1)
    )
    await cancelled_while_google_refused(
        api_client, db_engine, "rt-b", starts_in=dt.timedelta(days=2)
    )
    calendar = FakeCalendar()

    released = await retry(db_engine, calendar, limit=2)

    assert released == 2
    assert calendar.asked_to_cancel == ["evt-rt-a", "evt-rt-b"]
    assert await event_of(db_engine, last) == "evt-rt-c"


async def test_an_ended_session_with_no_event_takes_no_turn(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Most ended sessions released their event, or never had one; they are
    not candidates, so they cannot fill the run's bound."""
    released_already = await cancelled_while_google_refused(
        api_client, db_engine, "rt-none", starts_in=dt.timedelta(days=1)
    )
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET external_calendar_event_id = NULL WHERE id = :i"),
            {"i": released_already},
        )
    waiting = await cancelled_while_google_refused(
        api_client, db_engine, "rt-some", starts_in=dt.timedelta(days=2)
    )
    calendar = FakeCalendar()

    released = await retry(db_engine, calendar, limit=1)

    assert released == 1
    assert calendar.asked_to_cancel == ["evt-rt-some"]
    assert await event_of(db_engine, waiting) is None


def jobs_on(database: str) -> RuntimeJobs:
    return RuntimeJobs(Settings(_env_file=None, database_url=SecretStr(database)))


async def test_the_hourly_settlement_runs_the_retry(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = await cancelled_while_google_refused(api_client, db_engine, "rt-job")
    calendar = FakeCalendar()
    monkeypatch.setattr(runner, "calendar_for", lambda _s: guarded("calendar", calendar))

    result = await jobs_on(migrated_database).run("settle-sessions")

    assert result.counts["released_calendar_events"] == 1
    assert calendar.cancelled == ["evt-rt-job"]
    assert await event_of(db_engine, session_id) is None


async def test_a_dry_run_counts_what_it_would_retry_and_calls_nothing(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = await cancelled_while_google_refused(api_client, db_engine, "rt-dry")
    calendar = FakeCalendar()
    monkeypatch.setattr(runner, "calendar_for", lambda _s: guarded("calendar", calendar))

    result = await jobs_on(migrated_database).run("settle-sessions", dry_run=True)

    assert result.counts["released_calendar_events"] == 1
    assert calendar.asked_to_cancel == []
    assert await event_of(db_engine, session_id) == "evt-rt-dry"


async def test_with_no_calendar_configured_the_handle_is_kept(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, migrated_database: str
) -> None:
    """**Nothing to call, so nothing is cleared.** `NullCalendar` removes
    nothing and reports success; clearing the id on that would lose the only
    handle on an event a configured calendar could still remove."""
    session_id = await cancelled_while_google_refused(api_client, db_engine, "rt-null")

    result = await jobs_on(migrated_database).run("settle-sessions")

    assert result.counts["released_calendar_events"] == 0
    assert await event_of(db_engine, session_id) == "evt-rt-null"
