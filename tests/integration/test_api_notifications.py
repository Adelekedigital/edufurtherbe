"""Who gets told what, and what happens to the message afterwards.

**Two halves that must not be conflated.** The enqueue is a fact committed with
the thing that caused it, and the drain is a delivery attempt that may fail —
so a message can be owed without having been sent, and the table is what says
which. Tests here read the outbox directly rather than through a sender,
because the sender is the part that is allowed to be down.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.factories import add_availability, add_session_type, make_public_mentor

from app.domain.notifications import Channel, Notification
from app.infra.db.outbox import MAX_ATTEMPTS, drain
from app.infra.db.session_writer import expire_requests
from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


class Recorder:
    """A sender that says yes and remembers. Stands in for Loops."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, **kwargs: Any) -> None:
        self.sent.append(kwargs)


class Refuser:
    """A sender that is down."""

    def send(self, **kwargs: Any) -> None:
        del kwargs
        raise RuntimeError("provider unavailable")


async def a_booking(
    engine: AsyncEngine, client: httpx.AsyncClient, tag: str, *, confirmation: bool = False
) -> dict[str, Any]:
    mentor = await make_public_mentor(engine, tag)
    session_type = await add_session_type(engine, mentor, duration=60, notice=0)
    for day in range(7):
        await add_availability(engine, mentor, day_of_week=day, start="00:00", end="23:00")
    mentor_auth, mentee_auth = uuid4(), uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET auth_id = :a WHERE id = :u"), {"a": mentor_auth, "u": mentor}
        )
        mentee = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
                    "VALUES (:e, :a, 'Mo', 'mentee', 'Africa/Lagos') RETURNING id"
                ),
                {"e": f"mentee-{tag}@example.test", "a": mentee_auth},
            )
        ).scalar_one()
        if confirmation:
            await conn.execute(
                text(
                    "UPDATE mentor_profiles SET requires_booking_confirmation = true "
                    "WHERE user_id = :u"
                ),
                {"u": mentor},
            )
    slots = await client.get(
        f"/api/v1/users/{mentor}/availability/slots",
        params={"session_type_id": str(session_type)},
    )
    created = await client.post(
        "/api/v1/sessions",
        json={
            "session_type_id": str(session_type),
            "starts_at": str(slots.json()["data"][-1]["start"]),
        },
        headers=bearer(api_token(mentee_auth)) | {"Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 201, created.text
    return {
        "id": created.json()["id"],
        "mentor": mentor,
        "mentee": mentee,
        "mentor_headers": bearer(api_token(mentor_auth)),
        "mentee_headers": bearer(api_token(mentee_auth)),
    }


async def queued(engine: AsyncEngine, session_id: str) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT event_type, destination, status, attempts, payload, error_detail "
                        "FROM outbox_events WHERE entity_id = :i ORDER BY created_at"
                    ),
                    {"i": session_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def audience(rows: list[dict[str, Any]]) -> set[UUID]:
    return {UUID(str(row["payload"]["recipient_id"])) for row in rows}


async def sweep(engine: AsyncEngine, notifier: Any) -> dict[str, int]:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        counts = await drain(session, notifier=notifier, now=dt.datetime.now(dt.UTC))
        await session.commit()
    return counts


# --------------------------------------------------------------------------
# The enqueue, and who it names
# --------------------------------------------------------------------------


async def test_an_auto_confirming_booking_queues_one_message_for_the_mentor(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**One row, and the mentee is not in it.** They are looking at a
    confirmation screen; the mentor is the one learning something."""
    booking = await a_booking(db_engine, api_client, "nf-auto")

    rows = await queued(db_engine, booking["id"])

    assert [row["event_type"] for row in rows] == [Notification.SESSION_BOOKED]
    assert audience(rows) == {booking["mentor"]}


async def test_a_request_queues_for_the_mentor_who_has_to_answer_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(db_engine, api_client, "nf-request", confirmation=True)

    rows = await queued(db_engine, booking["id"])

    assert [row["event_type"] for row in rows] == [Notification.SESSION_REQUESTED]
    assert audience(rows) == {booking["mentor"]}


async def test_accepting_queues_for_the_mentee(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The same rule the other way round**, and the pair is what makes it a
    rule rather than a list: the mentor has just clicked, and the mentee has
    been waiting on exactly this."""
    booking = await a_booking(db_engine, api_client, "nf-accept", confirmation=True)

    await api_client.post(
        f"/api/v1/sessions/{booking['id']}/accept", headers=booking["mentor_headers"]
    )

    rows = await queued(db_engine, booking["id"])
    accepted = [row for row in rows if row["event_type"] == Notification.REQUEST_ACCEPTED]
    assert audience(accepted) == {booking["mentee"]}


async def test_a_cancellation_queues_for_whoever_did_not_cancel(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The only action either party may take, so the audience depends on the
    actor rather than on the message — asserted from both sides, because a
    version scoped to one would pass on whichever half it happened to pick."""
    by_mentor = await a_booking(db_engine, api_client, "nf-cancel-a")
    by_mentee = await a_booking(db_engine, api_client, "nf-cancel-b")

    await api_client.post(
        f"/api/v1/sessions/{by_mentor['id']}/cancel", headers=by_mentor["mentor_headers"]
    )
    await api_client.post(
        f"/api/v1/sessions/{by_mentee['id']}/cancel", headers=by_mentee["mentee_headers"]
    )

    theirs = [
        row
        for row in await queued(db_engine, by_mentor["id"])
        if row["event_type"] == Notification.SESSION_CANCELLED
    ]
    mine = [
        row
        for row in await queued(db_engine, by_mentee["id"])
        if row["event_type"] == Notification.SESSION_CANCELLED
    ]
    assert audience(theirs) == {by_mentor["mentee"]}
    assert audience(mine) == {by_mentee["mentor"]}


async def test_an_expiry_queues_for_both(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The rule's one exception.** Nobody acted, so neither party knows — the
    mentor let it lapse and the mentee has been waiting on an answer that is no
    longer coming."""
    booking = await a_booking(db_engine, api_client, "nf-expire", confirmation=True)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET respond_by = now() - interval '1 minute' WHERE id = :i"),
            {"i": booking["id"]},
        )
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as session:
        await expire_requests(session, now=dt.datetime.now(dt.UTC))
        await session.commit()

    expired = [
        row
        for row in await queued(db_engine, booking["id"])
        if row["event_type"] == Notification.REQUEST_EXPIRED
    ]

    assert audience(expired) == {booking["mentor"], booking["mentee"]}


async def test_the_message_is_committed_with_the_session_that_caused_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The whole reason the table exists.** There is no window where a booking
    exists and nobody will ever hear, and none where somebody hears about a
    booking that rolled back — so a session with no queued message is a defect
    rather than a race."""
    booking = await a_booking(db_engine, api_client, "nf-atomic")

    async with db_engine.connect() as conn:
        both = (
            (
                await conn.execute(
                    text(
                        "SELECT (SELECT count(*) FROM sessions WHERE id = :i) AS sessions, "
                        "(SELECT count(*) FROM outbox_events WHERE entity_id = :i) AS queued"
                    ),
                    {"i": booking["id"]},
                )
            )
            .mappings()
            .one()
        )

    assert both["sessions"] == 1
    assert both["queued"] >= 1


# --------------------------------------------------------------------------
# The drain
# --------------------------------------------------------------------------


async def test_draining_sends_and_marks_the_row(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(db_engine, api_client, "nf-send")
    recorder = Recorder()

    counts = await sweep(db_engine, recorder)

    assert counts["sent"] >= 1
    (message,) = [m for m in recorder.sent if m["notification"] == Notification.SESSION_BOOKED]
    assert message["channel"] is Channel.EMAIL
    assert "@" in message["to"]
    (row,) = await queued(db_engine, booking["id"])
    assert row["status"] == "sent"


async def test_a_second_drain_sends_nothing_again(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """An hourly sweep that resent everything it had ever sent would be a
    duplicate in somebody's inbox every hour."""
    await a_booking(db_engine, api_client, "nf-once")
    await sweep(db_engine, Recorder())

    second = Recorder()
    await sweep(db_engine, second)

    assert second.sent == []


async def test_the_idempotency_key_is_the_row_itself(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """So a retry after a timeout replays the provider's answer rather than
    sending a second copy."""
    booking = await a_booking(db_engine, api_client, "nf-key")
    recorder = Recorder()

    await sweep(db_engine, recorder)

    async with db_engine.connect() as conn:
        row_id = (
            await conn.execute(
                text("SELECT id FROM outbox_events WHERE entity_id = :i"), {"i": booking["id"]}
            )
        ).scalar_one()
    assert any(m["idempotency_key"] == str(row_id) for m in recorder.sent)


async def test_a_failed_send_is_retried_rather_than_lost(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Down is not gone.** The row goes back to pending with its attempt
    counted, so the next sweep tries again — which is the difference between an
    outbox and a log of things that did not happen."""
    booking = await a_booking(db_engine, api_client, "nf-retry")

    counts = await sweep(db_engine, Refuser())

    assert counts["failed"] == 1
    (row,) = await queued(db_engine, booking["id"])
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert "unavailable" in row["error_detail"]

    recovered = Recorder()
    await sweep(db_engine, recovered)
    assert len(recovered.sent) == 1


async def test_retrying_stops_at_the_bound(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**An unbounded retry is an outage amplifier**, and the row has to end
    somewhere a human can find it: `failed`, with the provider's last words on
    it, rather than pending forever."""
    booking = await a_booking(db_engine, api_client, "nf-bound")

    for _ in range(MAX_ATTEMPTS + 2):
        await sweep(db_engine, Refuser())

    (row,) = await queued(db_engine, booking["id"])
    assert row["status"] == "failed"
    assert row["attempts"] == MAX_ATTEMPTS


async def test_a_recipient_with_no_address_is_skipped_not_failed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Two different questions.** Failed means the provider said no; skipped
    means there was never anywhere to send it. Conflating them would burn the
    retry budget on a message that can never be delivered, and would hide a data
    problem inside a delivery one.

    WhatsApp is the channel that produces it today, because nobody has a phone
    number.
    """
    booking = await a_booking(db_engine, api_client, "nf-noaddress")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE outbox_events SET destination = 'whatsapp' WHERE entity_id = :i"),
            {"i": booking["id"]},
        )

    counts = await sweep(db_engine, Recorder())

    assert counts["skipped"] == 1
    (row,) = await queued(db_engine, booking["id"])
    assert row["status"] == "skipped"


async def test_a_drain_with_no_provider_records_rather_than_loses(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The state of every environment until a key is set. The `NullNotifier`
    accepts, so the row is marked sent and nothing is owed — which is honest:
    the platform decided not to send it, rather than failing to."""
    from app.infra.clients.notifications import NullNotifier

    booking = await a_booking(db_engine, api_client, "nf-nokey")

    counts = await sweep(db_engine, NullNotifier())

    assert counts["sent"] == 1
    (row,) = await queued(db_engine, booking["id"])
    assert row["status"] == "sent"
