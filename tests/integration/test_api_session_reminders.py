"""Nudging before a session, and asking after one that happened.

**These are a regression against the legacy application, not a new feature.**
The Loops account carries *Session Reminder*, *Session Last Reminder* and
*Session Feedback*, so the app being replaced sends all three. The only reminder
this platform had chased a mentor to *answer a request*, measured back from
`respond_by` — a different thing entirely.

**There is no post-session message here any more.** One was built and
withdrawn: it conflated a *platform* survey to both parties with a *mentor
review* from the mentee, and only the second is wanted. That one belongs to the
review phase and is `Audience.MENTEE`.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.test_api_notifications import a_booking, queued

from app.core.config import Settings
from app.domain.notifications import SESSION_REMINDERS, Notification
from app.infra.clients.scheduler import SchedulerError
from app.infra.db.session_writer import remind_before_session, schedule_session_reminders

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


class Publisher:
    """A scheduler that says yes and remembers what it was asked to publish."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    def schedule(self, *, url: str, body: dict[str, Any], at: dt.datetime) -> None:
        self.published.append({"url": url, "body": body, "at": at})


class Refusing:
    """A scheduler that is down.

    Raises `SchedulerError` specifically, because that is what the adapters
    raise and what the caller catches. A bare `RuntimeError` would propagate and
    take the confirmation with it — which is the existing contract for booking
    too, and matching it deliberately rather than widening the catch here.
    """

    def schedule(self, **kwargs: Any) -> None:
        del kwargs
        raise SchedulerError("scheduler unavailable")


async def fire(engine: AsyncEngine, session_id: str, kind: str) -> bool:
    """The callback, without the signature check the route does first."""
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        queued_it = await remind_before_session(session, UUID(session_id), kind)
        await session.commit()
    return queued_it


async def set_status(engine: AsyncEngine, session_id: str, status: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET status = :s WHERE id = :i"), {"s": status, "i": session_id}
        )


def kinds_of(rows: list[dict[str, Any]]) -> list[str]:
    return [str(row["event_type"]) for row in rows]


# --------------------------------------------------------------------------
# Scheduling
# --------------------------------------------------------------------------


def test_both_nudges_are_published_for_a_session_far_enough_out() -> None:
    publisher = Publisher()
    starts_at = dt.datetime(2026, 9, 10, 12, 0, tzinfo=dt.UTC)

    count = schedule_session_reminders(
        uuid4(),
        starts_at,
        scheduler=publisher,
        callback_url="https://api.example.test/callbacks/reminders",
        now=starts_at - dt.timedelta(days=5),
    )

    assert count == 2
    assert [job["body"]["kind"] for job in publisher.published] == ["s24", "s1"]
    assert publisher.published[0]["at"] == starts_at - dt.timedelta(hours=24)
    assert publisher.published[1]["at"] == starts_at - dt.timedelta(hours=1)


def test_a_nudge_whose_moment_has_passed_is_dropped_rather_than_fired() -> None:
    """**The booking floor is 24 hours**, so a session booked at the minimum
    notice has its 24-hour reminder already behind it. Firing it on confirmation
    would be the confirmation message again, saying the session is tomorrow when
    the mentee just booked it for tomorrow."""
    publisher = Publisher()
    starts_at = dt.datetime(2026, 9, 10, 12, 0, tzinfo=dt.UTC)

    schedule_session_reminders(
        uuid4(),
        starts_at,
        scheduler=publisher,
        callback_url="https://api.example.test/callbacks/reminders",
        now=starts_at - dt.timedelta(hours=24),
    )

    assert [job["body"]["kind"] for job in publisher.published] == ["s1"]


def test_a_scheduler_that_is_down_does_not_take_the_confirmation_with_it() -> None:
    """The session exists and holds its slot; the parties are simply not nudged.
    Refusing a confirmation because a scheduler was slow loses something
    unrecoverable to protect something that is not."""
    assert (
        schedule_session_reminders(
            uuid4(),
            dt.datetime(2026, 9, 10, 12, 0, tzinfo=dt.UTC),
            scheduler=Refusing(),
            callback_url="https://api.example.test/callbacks/reminders",
            now=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
        )
        == 0
    )


def test_an_unconfigured_scheduler_publishes_nothing_quietly() -> None:
    assert (
        schedule_session_reminders(
            uuid4(),
            dt.datetime(2026, 9, 10, 12, 0, tzinfo=dt.UTC),
            scheduler=None,
            callback_url="",
            now=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
        )
        == 0
    )


# --------------------------------------------------------------------------
# Both confirmation points, through the real endpoints
# --------------------------------------------------------------------------


def wire(client: httpx.AsyncClient) -> Publisher:
    """A scheduler on `app.state`, the way a composition root would put one."""
    publisher = Publisher()
    app = client._transport.app  # type: ignore[attr-defined]
    app.state.scheduler = publisher
    app.state.settings = Settings(_env_file=None, public_base_url="https://api.example.test")
    return publisher


async def test_booking_an_auto_confirming_offering_publishes_its_reminders(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A session confirmed outright is real now, so its nudges go out now."""
    publisher = wire(api_client)

    await a_booking(db_engine, api_client, "sr-auto")

    session_kinds = [j["body"]["kind"] for j in publisher.published if j["body"]["kind"][0] == "s"]
    assert session_kinds, "an auto-confirmed session published no reminders"


async def test_a_request_nobody_has_accepted_publishes_none(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The bug this branch exists to prevent.** Telling both parties their
    session is tomorrow, for a request nobody has agreed to, is worse than
    telling them nothing."""
    publisher = wire(api_client)

    await a_booking(db_engine, api_client, "sr-waits", confirmation=True)

    assert [j for j in publisher.published if j["body"]["kind"][0] == "s"] == []


async def test_accepting_a_request_publishes_them_then(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The second confirmation point**, and the one easiest to miss. Without
    it every confirmation-required session would be silently unreminded — the
    same shape of gap that made `release_meeting` necessary."""
    publisher = wire(api_client)
    booking = await a_booking(db_engine, api_client, "sr-accept", confirmation=True)
    publisher.published.clear()

    response = await api_client.post(
        f"/api/v1/sessions/{booking['id']}/accept", headers=booking["mentor_headers"], json={}
    )

    assert response.status_code == 200, response.text
    assert [j["body"]["kind"] for j in publisher.published if j["body"]["kind"][0] == "s"] == [
        "s24",
        "s1",
    ]


# --------------------------------------------------------------------------
# Firing
# --------------------------------------------------------------------------


async def test_a_confirmed_session_nudges_both_parties(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Either party can forget, and the message is about turning up."""
    booking = await a_booking(db_engine, api_client, "sr-both")

    assert await fire(db_engine, booking["id"], "s24") is True
    rows = [r for r in await queued(db_engine, booking["id"]) if "reminder" in str(r["event_type"])]

    assert kinds_of(rows) == ["session_reminder", "session_reminder"], "one row per recipient"
    assert {row["payload"]["interval"] for row in rows} == {"24 hours"}


async def test_the_last_nudge_is_its_own_message(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A separate member rather than the same one with a different interval:
    the templates differ, because one is about preparing and one about
    turning up."""
    booking = await a_booking(db_engine, api_client, "sr-last")

    await fire(db_engine, booking["id"], "s1")
    rows = [r for r in await queued(db_engine, booking["id"]) if "reminder" in str(r["event_type"])]

    assert set(kinds_of(rows)) == {"session_last_reminder"}
    assert {row["payload"]["interval"] for row in rows} == {"1 hour"}


@pytest.mark.parametrize("status", ["cancelled", "declined", "withdrawn", "expired"])
async def test_a_session_that_is_no_longer_happening_nudges_nobody(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, status: str
) -> None:
    """**Nothing is ever cancelled — the callback re-reads.**

    Making four transitions responsible for unscheduling is how a reminder ends
    up arriving for a session called off through the one path somebody forgot.
    """
    booking = await a_booking(db_engine, api_client, f"sr-{status}")
    await set_status(db_engine, booking["id"], status)

    assert await fire(db_engine, booking["id"], "s24") is False
    assert [
        r for r in await queued(db_engine, booking["id"]) if "reminder" in str(r["event_type"])
    ] == []


async def test_a_pending_request_is_not_nudged_about_a_session(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Telling both parties their session is tomorrow, for a request nobody has
    accepted, is the failure the confirmation branch exists to avoid."""
    booking = await a_booking(db_engine, api_client, "sr-pending", confirmation=True)

    assert await fire(db_engine, booking["id"], "s24") is False


async def test_firing_twice_queues_one_message_each(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """QStash retries by design, so two callbacks arriving together both see a
    confirmed session — the partial unique index is what makes the second a
    no-op, not the re-read."""
    booking = await a_booking(db_engine, api_client, "sr-twice")

    await fire(db_engine, booking["id"], "s24")
    await fire(db_engine, booking["id"], "s24")

    rows = [r for r in await queued(db_engine, booking["id"]) if "reminder" in str(r["event_type"])]
    assert len(rows) == 2, "one per recipient, not two per recipient"


# --------------------------------------------------------------------------
# The index that makes two recipients possible
# --------------------------------------------------------------------------


async def test_the_reminder_index_distinguishes_recipients(db_engine: AsyncEngine) -> None:
    """**Asserted against the live index, because nothing else can be.**

    `alembic check` skips expression indexes — it says so itself for
    `ix_institutions_name_prefix` — so the model and the migration can disagree
    with every gate green. This reads what the database actually has.

    The column is load-bearing rather than decorative. Without it the index is
    `(entity_id, event_type, kind)`, which is right for a message with one
    recipient and silently drops the second row for a message with two: one
    party reminded, the other not, `ON CONFLICT DO NOTHING` saying nothing. It
    was correct when written — the only reminder then went to the mentor alone.
    """
    async with db_engine.connect() as conn:
        sql = (
            await conn.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE schemaname = 'public' AND indexname = :name"
                ),
                {"name": "uq_outbox_events_reminder"},
            )
        ).scalar_one()

    assert "UNIQUE" in sql
    assert "recipient_id" in sql, "two recipients would collapse into one row"
    assert "kind" in sql
    # Partial, so it constrains nothing but reminders.
    assert "WHERE" in sql


# --------------------------------------------------------------------------
# The schedule itself
# --------------------------------------------------------------------------


def test_every_reminder_carries_the_words_its_template_renders() -> None:
    """`intervaltime` travels with the offset rather than being derived at send
    time, so changing one moves the other — and a message cannot say "24 hours"
    an hour before."""
    assert [(r.kind, r.interval) for r in SESSION_REMINDERS] == [
        ("s24", "24 hours"),
        ("s1", "1 hour"),
    ]


def test_the_two_reminders_are_different_messages() -> None:
    assert {r.notification for r in SESSION_REMINDERS} == {
        Notification.SESSION_REMINDER,
        Notification.SESSION_LAST_REMINDER,
    }
