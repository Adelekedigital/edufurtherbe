"""Google free/busy has one budget per process; past it, reads fail open
(decision 242). No lockout and no 429: the same degradation as a Google outage.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.test_api_freebusy import (
    DAY,
    KEY,
    FakeGoogle,
    a_mentee_for,
    book_at,
    configured,
    connect_calendar,
    read_slots,
)
from tests.integration.test_api_slots import at, make_mentor
from tests.off_the_loop import guarded

from app.api.deps.calendar import _free_busy
from app.domain.availability import UtcInterval
from app.domain.rate_limits import SlidingWindow
from app.infra.clients.meetings import VenueUnavailableError
from app.infra.db.calendar_store import MentorFreeBusy
from app.main import create_app

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

BUSY = (UtcInterval(at(DAY, "08:45"), at(DAY, "09:30")),)


def spent() -> SlidingWindow:
    """A budget of one, already used, with a window no test outlives."""
    budget = SlidingWindow(limit=1, window_seconds=1e9)
    budget.allow(now=time.monotonic())
    return budget


def wire_budgeted(client: httpx.AsyncClient, google: FakeGoogle, budget: SlidingWindow) -> None:
    app = client._transport.app  # type: ignore[attr-defined]
    app.state.settings = configured()
    app.state.free_busy = MentorFreeBusy(
        client_id="cid",
        client_secret="gcs",  # noqa: S106
        key=KEY,
        reader=guarded("free/busy reader", google),
        session_factory=app.state.session_factory,
        budget=budget,
    )


async def test_past_the_budget_a_read_skips_google_and_fails_open(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, session_type = await make_mentor(db_engine, "fb-budget")
    await connect_calendar(db_engine, mentor)
    google = FakeGoogle(busy=BUSY)
    wire_budgeted(api_client, google, SlidingWindow(limit=1, window_seconds=60))

    first = await read_slots(api_client, mentor, session_type)
    second = await read_slots(api_client, mentor, session_type)

    assert len(google.calls) == 1
    assert at(DAY, "08:45") not in first
    # Answered from declared hours, exactly as in an outage: never a 429.
    assert at(DAY, "08:45") in second


async def test_a_mentor_with_no_calendar_spends_none_of_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Only a real Google call counts, so cheap reads cannot drain it."""
    unconnected, plain_type = await make_mentor(db_engine, "fb-budget-plain")
    connected, session_type = await make_mentor(db_engine, "fb-budget-cal")
    await connect_calendar(db_engine, connected)
    google = FakeGoogle(busy=BUSY)
    wire_budgeted(api_client, google, SlidingWindow(limit=1, window_seconds=60))

    for _ in range(3):
        await read_slots(api_client, unconnected, plain_type)
    starts = await read_slots(api_client, connected, session_type)

    assert len(google.calls) == 1
    assert at(DAY, "08:45") not in starts


async def test_a_caller_that_must_not_fail_open_is_told(db_engine: AsyncEngine) -> None:
    """The next-free-time job stores what it reads, so it raises instead."""
    mentor, _ = await make_mentor(db_engine, "fb-budget-job")
    await connect_calendar(db_engine, mentor)
    google = FakeGoogle(busy=BUSY)
    reader = MentorFreeBusy(
        client_id="cid",
        client_secret="gcs",  # noqa: S106
        key=KEY,
        reader=google,
        fail_open=False,
        budget=spent(),
    )

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as session:
        with pytest.raises(VenueUnavailableError):
            await reader.busy(session, mentor, at(DAY, "08:00"), at(DAY, "12:00"))

    assert google.calls == []


def real_wiring(
    client: httpx.AsyncClient, google: FakeGoogle, monkeypatch: pytest.MonkeyPatch
) -> SlidingWindow:
    """The app's own `_free_busy`, with only Google faked, and its budget spent.

    The other tests hand the app a reader; this one leaves `app.state.free_busy`
    unset so the wiring that decides who is budgeted is the code under test.
    """
    app = client._transport.app  # type: ignore[attr-defined]
    app.state.settings = configured()
    defaults = MentorFreeBusy.__init__.__kwdefaults__
    assert defaults is not None
    monkeypatch.setitem(defaults, "reader", guarded("free/busy reader", google))
    app.state.free_busy_budget = spent()
    return app.state.free_busy_budget


async def test_a_spent_budget_skips_google_for_the_public_slot_read(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    mentor, session_type = await make_mentor(db_engine, "fb-real-slots")
    await connect_calendar(db_engine, mentor)
    google = FakeGoogle(busy=BUSY)
    real_wiring(api_client, google, monkeypatch)

    starts = await read_slots(api_client, mentor, session_type)

    assert google.calls == []
    assert at(DAY, "08:45") in starts


async def test_a_spent_budget_never_lets_a_booking_skip_the_calendar(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An anonymous flood drains the slot read's budget, not booking's: a real
    booking still asks Google and is refused over the mentor's commitment."""
    mentor, session_type = await make_mentor(db_engine, "fb-real-book", notice=0)
    await connect_calendar(db_engine, mentor)
    mentee_auth = await a_mentee_for(db_engine, "fb-real-book")
    google = FakeGoogle(busy=BUSY)
    real_wiring(api_client, google, monkeypatch)

    response = await book_at(api_client, session_type, mentee_auth, at(DAY, "08:45"))

    assert response.status_code == 422
    assert google.calls


async def test_the_app_shares_one_budget_across_its_requests(db_engine: AsyncEngine) -> None:
    app = create_app(configured().model_copy(update={"free_busy_calls_per_minute": 7}))
    app.state.session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    request = SimpleNamespace(app=app)

    first = _free_busy(request, budgeted=True)  # type: ignore[arg-type]
    second = _free_busy(request, budgeted=True)  # type: ignore[arg-type]
    booking = _free_busy(request)  # type: ignore[arg-type]

    assert isinstance(first, MentorFreeBusy)
    assert first._budget is second._budget is app.state.free_busy_budget
    assert booking._budget is None
    assert app.state.free_busy_budget.limit == 7
    assert app.state.free_busy_budget.window_seconds == 60.0


async def test_a_spent_budget_is_reported_once_not_per_request(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flood must not become a log flood."""
    from app.infra.db import calendar_store

    mentor, session_type = await make_mentor(db_engine, "fb-real-log")
    await connect_calendar(db_engine, mentor)
    real_wiring(api_client, FakeGoogle(busy=BUSY), monkeypatch)
    warnings: list[tuple[object, ...]] = []
    monkeypatch.setattr(calendar_store.logger, "warning", lambda *a, **_k: warnings.append(a))

    for _ in range(3):
        await read_slots(api_client, mentor, session_type)

    assert len(warnings) == 1
