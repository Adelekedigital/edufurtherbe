"""A calendar event an ending could not remove is retried by the hourly run (#423).

`release_meeting` keeps `external_calendar_event_id` when Google refuses, so
that a later run can try again. These pin the later run: the settlement job
retries ended sessions that still carry an event until the session would have
ended, a bounded number per run in a fair order and inside the job's time
budget, and never touches a session that is still on.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from collections import Counter
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tests.integration.meeting_fakes import FakeCalendar, FakeRooms
from tests.integration.test_api_meeting_provisioning import a_mentor_on, book, venue_of
from tests.integration.test_api_response_deadline import a_request, make_overdue
from tests.off_the_loop import guarded

from app.core.config import Settings
from app.infra.db.session_writer import retry_releases
from app.infra.db.session_writer.follow_ups import RELEASE_ROW_ALLOWANCE_SECONDS
from app.infra.jobs import runner
from app.infra.jobs.runner import RuntimeJobs
from conftest import A_REASON

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

PAUSED = "calendar releases paused"


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> Any:
    """`caplog`, with the two loggers read here switched back on: Alembic's
    `fileConfig` disables every logger that existed when the schema migrated."""
    for name in ("app.infra.db.session_writer.meetings", "app.infra.db.session_writer.follow_ups"):
        monkeypatch.setattr(logging.getLogger(name), "disabled", False)
    caplog.set_level(logging.INFO)
    return caplog


async def event_of(engine: AsyncEngine, session_id: str) -> str | None:
    value = (await venue_of(engine, session_id))["external_calendar_event_id"]
    return None if value is None else str(value)


async def set_event(engine: AsyncEngine, session_id: str, event_id: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET external_calendar_event_id = :e WHERE id = :i"),
            {"e": event_id, "i": session_id},
        )


async def move_start(engine: AsyncEngine, session_id: str, by: dt.timedelta) -> None:
    """Start the session `by` from now: the run happening later than the ending."""
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET starts_at = :s WHERE id = :i"),
            {"s": dt.datetime.now(dt.UTC) + by, "i": session_id},
        )


async def booked_with_event(
    api_client: httpx.AsyncClient, engine: AsyncEngine, tag: str, *, starts_in: dt.timedelta
) -> tuple[str, dict[str, Any]]:
    """A confirmed one-hour session with its own event id, starting `starts_in`
    from now."""
    app = api_client._transport.app  # type: ignore[attr-defined]
    app.state.meeting_rooms = FakeRooms()
    app.state.calendar = FakeCalendar(refuses_cancel=True)
    setup = await a_mentor_on(engine, tag, "daily")
    session = await book(api_client, setup)
    await set_event(engine, session["id"], f"evt-{tag}")
    await move_start(engine, session["id"], starts_in)
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


async def retry_in(session: AsyncSession, calendar: FakeCalendar, **kwargs: Any) -> int:
    now = dt.datetime.now(dt.UTC)
    return await retry_releases(
        session,
        now=now,
        calendar=guarded("calendar", calendar),
        **({"deadline": math.inf, "untouched_since": now} | kwargs),
    )


async def retry(engine: AsyncEngine, calendar: FakeCalendar, **kwargs: Any) -> int:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        return await retry_in(session, calendar, **kwargs)


# --------------------------------------------------------------------------
# Which sessions, and what a refusal does
# --------------------------------------------------------------------------


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
    for the next hour; the other is still removed."""
    stuck = await cancelled_while_google_refused(api_client, db_engine, "rt-stuck")
    other = await cancelled_while_google_refused(api_client, db_engine, "rt-other")
    calendar = FakeCalendar(refuses_cancel=frozenset({"evt-rt-stuck"}))

    released = await retry(db_engine, calendar)

    assert sorted(calendar.asked_to_cancel) == ["evt-rt-other", "evt-rt-stuck"]
    assert released == 1
    assert await event_of(db_engine, stuck) == "evt-rt-stuck"
    assert await event_of(db_engine, other) is None


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


async def test_a_session_cancelled_just_before_its_start_is_still_retried_once_started(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Until the session would have ended, not until it starts.** Cancelled
    half an hour before its start while Google refused, the next hourly run
    comes after the start; the invite still says the hour is on."""
    session_id = await cancelled_while_google_refused(api_client, db_engine, "rt-late")
    await move_start(db_engine, session_id, -dt.timedelta(minutes=10))
    calendar = FakeCalendar()

    released = await retry(db_engine, calendar)

    assert released == 1
    assert calendar.cancelled == ["evt-rt-late"]
    assert await event_of(db_engine, session_id) is None


async def test_an_event_whose_session_has_ended_is_left(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Not after its end.** Nobody can turn up to it any more, and deleting
    the event would email both parties a cancellation of something already
    over — every migrated ended session with an event id would get one."""
    session_id = await cancelled_while_google_refused(api_client, db_engine, "rt-past")
    await move_start(db_engine, session_id, -dt.timedelta(minutes=61))
    calendar = FakeCalendar()

    released = await retry(db_engine, calendar)

    assert released == 0
    assert calendar.asked_to_cancel == []
    assert await event_of(db_engine, session_id) == "evt-rt-past"


async def test_an_ended_session_with_no_event_takes_no_turn(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Most ended sessions released their event, or never had one; they are
    not candidates, so they cannot fill the run's bound."""
    released_already = await cancelled_while_google_refused(api_client, db_engine, "rt-none")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET external_calendar_event_id = NULL WHERE id = :i"),
            {"i": released_already},
        )
    waiting = await cancelled_while_google_refused(api_client, db_engine, "rt-some")
    calendar = FakeCalendar()

    released = await retry(db_engine, calendar, limit=1)

    assert released == 1
    assert calendar.asked_to_cancel == ["evt-rt-some"]
    assert await event_of(db_engine, waiting) is None


async def test_a_session_ended_during_this_run_is_not_tried_twice(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A row written since the run began was just tried by its ending; the
    retry is for the ones an earlier run left."""
    earlier = await cancelled_while_google_refused(api_client, db_engine, "rt-earlier")
    started = dt.datetime.now(dt.UTC)
    just_now = await cancelled_while_google_refused(api_client, db_engine, "rt-just-now")
    calendar = FakeCalendar()

    released = await retry(db_engine, calendar, untouched_since=started)

    assert released == 1
    assert calendar.asked_to_cancel == ["evt-rt-earlier"]
    assert await event_of(db_engine, earlier) is None
    assert await event_of(db_engine, just_now) == "evt-rt-just-now"


# --------------------------------------------------------------------------
# How much one run does
# --------------------------------------------------------------------------


async def test_a_run_retries_a_bounded_number(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Bounded, so a long backlog cannot run the job out of time.**"""
    ids = [
        await cancelled_while_google_refused(api_client, db_engine, f"rt-{tag}") for tag in "abc"
    ]
    calendar = FakeCalendar()

    released = await retry(db_engine, calendar, limit=2)

    assert released == 2
    assert len(calendar.asked_to_cancel) == 2
    assert [await event_of(db_engine, i) for i in ids].count(None) == 2


async def test_a_row_behind_permanent_refusals_still_gets_a_turn(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Fair, so refusals cannot hold every slot.** Two events Google always
    refuses start sooner than the one it would remove; with a bound of two, an
    order that favours them never reaches it. Seeded, so the order is the same
    on every run of this test."""
    for tag in ("rt-refused-1", "rt-refused-2"):
        await cancelled_while_google_refused(
            api_client, db_engine, tag, starts_in=dt.timedelta(days=1)
        )
    waiting = await cancelled_while_google_refused(
        api_client, db_engine, "rt-waiting", starts_in=dt.timedelta(days=3)
    )
    calendar = FakeCalendar(refuses_cancel=frozenset({"evt-rt-refused-1", "evt-rt-refused-2"}))

    async with db_engine.connect() as conn:
        await conn.execute(text("SELECT setseed(0.5)"))
        await conn.commit()
        session = AsyncSession(bind=conn, expire_on_commit=False)
        for _ in range(20):
            await retry_in(session, calendar, limit=2)
            if await event_of(db_engine, waiting) is None:
                break

    assert calendar.cancelled == ["evt-rt-waiting"]


async def test_no_row_is_started_once_the_budget_cannot_cover_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Paced by the clock, not only by the count.** The first row starts with
    exactly one allowance left; by the second the clock has passed it."""
    for tag in ("rt-t1", "rt-t2"):
        await cancelled_while_google_refused(api_client, db_engine, tag)
    readings = iter([0.0, 1.0])
    calendar = FakeCalendar()

    released = await retry(
        db_engine,
        calendar,
        deadline=RELEASE_ROW_ALLOWANCE_SECONDS,
        clock=lambda: next(readings),
    )

    assert released == 1
    assert len(calendar.asked_to_cancel) == 1


async def test_a_run_with_no_budget_left_starts_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    session_id = await cancelled_while_google_refused(api_client, db_engine, "rt-late-run")
    calendar = FakeCalendar()

    released = await retry(
        db_engine, calendar, deadline=RELEASE_ROW_ALLOWANCE_SECONDS - 1, clock=lambda: 0.0
    )

    assert released == 0
    assert calendar.asked_to_cancel == []
    assert await event_of(db_engine, session_id) == "evt-rt-late-run"


async def test_one_second_left_is_not_enough_for_a_google_call(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**A row needs time for its call, not merely time on the clock.** A
    literal second, so the test does not move with the allowance it checks."""
    session_id = await cancelled_while_google_refused(api_client, db_engine, "rt-one-sec")
    calendar = FakeCalendar()

    released = await retry(db_engine, calendar, deadline=1.0, clock=lambda: 0.0)

    assert released == 0
    assert calendar.asked_to_cancel == []
    assert await event_of(db_engine, session_id) == "evt-rt-one-sec"


# --------------------------------------------------------------------------
# A dead platform grant
# --------------------------------------------------------------------------


async def test_a_dead_platform_grant_pauses_the_run_with_one_warning(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, logs: pytest.LogCaptureFixture
) -> None:
    """**Every other row would fail the same way**, so the run stops after the
    first and says so once; both ids stay for when the grant is replaced."""
    ids = [
        await cancelled_while_google_refused(api_client, db_engine, f"rt-dead-{n}") for n in (1, 2)
    ]
    calendar = FakeCalendar(revoked=True)

    released = await retry(db_engine, calendar)

    assert released == 0
    assert len(calendar.asked_to_cancel) == 1
    assert [await event_of(db_engine, i) for i in ids] == ["evt-rt-dead-1", "evt-rt-dead-2"]
    paused = [r for r in logs.records if PAUSED in r.getMessage()]
    assert len(paused) == 1
    assert paused[0].levelno == logging.WARNING


async def test_an_ending_on_a_dead_grant_warns_and_keeps_the_id(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, logs: pytest.LogCaptureFixture
) -> None:
    """**A warning, not the info a slow Google gets**: the grant will not come
    back on its own, so somebody has to see it."""
    session_id, setup = await booked_with_event(
        api_client, db_engine, "rt-dead-req", starts_in=dt.timedelta(days=1)
    )
    app = api_client._transport.app  # type: ignore[attr-defined]
    app.state.calendar = FakeCalendar(revoked=True)

    cancelled = await api_client.post(
        f"/api/v1/sessions/{session_id}/cancel", headers=setup["mentee_headers"], json=A_REASON
    )

    assert cancelled.status_code == 200, cancelled.text
    assert await event_of(db_engine, session_id) == "evt-rt-dead-req"
    assert any(r.levelno == logging.WARNING and "revoked" in r.getMessage() for r in logs.records)


async def test_an_ending_google_merely_refused_stays_at_info(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, logs: pytest.LogCaptureFixture
) -> None:
    await cancelled_while_google_refused(api_client, db_engine, "rt-slow")

    removal = [r for r in logs.records if "not removed" in r.getMessage()]
    assert removal
    assert all(r.levelno == logging.INFO for r in removal)


# --------------------------------------------------------------------------
# The hourly job
# --------------------------------------------------------------------------


def jobs_on(database: str) -> RuntimeJobs:
    return RuntimeJobs(Settings(_env_file=None, database_url=SecretStr(database)))


def calendar_in_runner(monkeypatch: pytest.MonkeyPatch, calendar: FakeCalendar) -> None:
    monkeypatch.setattr(runner, "calendar_for", lambda _s: guarded("calendar", calendar))


async def overdue_request_with_event(
    api_client: httpx.AsyncClient, engine: AsyncEngine, tag: str
) -> str:
    request = await a_request(engine, api_client, tag)
    await make_overdue(engine, request["id"])
    await set_event(engine, request["id"], f"evt-{tag}")
    return str(request["id"])


async def test_the_hourly_settlement_runs_the_retry(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = await cancelled_while_google_refused(api_client, db_engine, "rt-job")
    calendar = FakeCalendar()
    calendar_in_runner(monkeypatch, calendar)

    result = await jobs_on(migrated_database).run("settle-sessions")

    assert result.counts["released_calendar_events"] == 1
    assert calendar.cancelled == ["evt-rt-job"]
    assert await event_of(db_engine, session_id) is None


async def test_the_settlement_paces_the_retry_by_its_schedule_budget(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deadline is the job's QStash budget; with none left, no row starts."""
    session_id = await cancelled_while_google_refused(api_client, db_engine, "rt-budget")
    calendar = FakeCalendar()
    calendar_in_runner(monkeypatch, calendar)
    monkeypatch.setattr(runner, "SETTLE_SESSIONS_BUDGET", dt.timedelta(0))

    result = await jobs_on(migrated_database).run("settle-sessions")

    assert result.counts["released_calendar_events"] == 0
    assert calendar.asked_to_cancel == []
    assert await event_of(db_engine, session_id) == "evt-rt-budget"


async def test_a_request_the_sweep_just_tried_is_not_tried_again_in_the_same_run(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The expiry sweep's own release attempt counts as this run's try; one
    ended by an earlier run is still retried."""
    request = await overdue_request_with_event(api_client, db_engine, "rt-swept")
    await cancelled_while_google_refused(api_client, db_engine, "rt-before")
    calendar = FakeCalendar(refuses_cancel=True)
    calendar_in_runner(monkeypatch, calendar)

    result = await jobs_on(migrated_database).run("settle-sessions")

    assert result.counts["expired_requests"] == 1
    assert Counter(calendar.asked_to_cancel) == {"evt-rt-swept": 1, "evt-rt-before": 1}
    assert await event_of(db_engine, request) == "evt-rt-swept"


async def test_a_dry_run_calls_no_calendar_in_either_phase(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**A dry run deletes nothing in Google.** Rolling back the database does
    not roll back an email Google sent to both parties."""
    request = await overdue_request_with_event(api_client, db_engine, "rt-dry-req")
    session_id = await cancelled_while_google_refused(api_client, db_engine, "rt-dry")
    calendar = FakeCalendar()
    calendar_in_runner(monkeypatch, calendar)

    result = await jobs_on(migrated_database).run("settle-sessions", dry_run=True)

    assert result.counts["expired_requests"] == 1
    assert result.counts["released_calendar_events"] == 1
    assert calendar.asked_to_cancel == []
    assert await event_of(db_engine, request) == "evt-rt-dry-req"
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
