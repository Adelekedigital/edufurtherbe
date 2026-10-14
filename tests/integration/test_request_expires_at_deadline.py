"""A request expires at its deadline, not up to an hour later (owner, 2026-10-13).

The hourly sweep was the only thing that expired an unanswered request, so for
up to an hour a dead request was stored, listed and cancellable as pending, and
its hour stayed held. Booking now schedules a callback just past the deadline
that expires that one request through the sweep's own code (status, event,
refund, email, calendar). The sweep stays as the backstop for a lost callback
and for requests with no scheduled one.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_reminder_callback import (
    a_pending_request,
    believing_client,
    fire,
)
from tests.integration.test_api_session_reminders import wire

from app.domain.sessions import EXPIRY_GRACE, EXPIRY_KIND

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def row(engine: AsyncEngine, session_id: str) -> dict[str, Any]:
    async with engine.begin() as conn:
        found = (
            (
                await conn.execute(
                    text(
                        "SELECT s.status, s.respond_by, "
                        " (SELECT count(*) FROM session_events e WHERE e.session_id = s.id "
                        "  AND e.to_status = 'expired') AS events, "
                        " (SELECT count(*) FROM outbox_events o WHERE o.entity_id = s.id "
                        "  AND o.event_type = 'request_expired') AS emails "
                        "FROM sessions s WHERE s.id = :i"
                    ),
                    {"i": session_id},
                )
            )
            .mappings()
            .one()
        )
    return dict(found)


async def past_deadline(engine: AsyncEngine, session_id: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET respond_by = now() - interval '1 minute' WHERE id = :i"),
            {"i": session_id},
        )


async def test_booking_a_request_schedules_its_expiry_just_past_the_deadline(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    publisher = wire(api_client)

    request = await a_pending_request(db_engine, api_client, "exp-schedule")

    expiries = [j for j in publisher.published if j["body"]["kind"] == EXPIRY_KIND]
    deadline = (await row(db_engine, request["id"]))["respond_by"]
    assert len(expiries) == 1
    assert expiries[0]["body"]["session_id"] == request["id"]
    assert expiries[0]["at"] == deadline + EXPIRY_GRACE


async def test_the_expiry_callback_expires_a_lapsed_request(db_engine: AsyncEngine) -> None:
    async with believing_client(db_engine) as client:
        request = await a_pending_request(db_engine, client, "exp-fire")
        await past_deadline(db_engine, request["id"])

        fired = await fire(client, request["id"], kind=EXPIRY_KIND)

    assert fired.status_code == 200, fired.text
    after = await row(db_engine, request["id"])
    assert after["status"] == "expired"
    assert after["events"] == 1
    # Both parties are told (nobody acted, so neither knows): one row each.
    assert after["emails"] == 2


async def test_an_early_or_late_callback_changes_nothing(db_engine: AsyncEngine) -> None:
    """**Re-checked when it fires**: before the deadline nothing happens (the
    sweep or a later callback will), and a request already answered is left as
    it is."""
    async with believing_client(db_engine) as client:
        waiting = await a_pending_request(db_engine, client, "exp-early")
        answered = await a_pending_request(db_engine, client, "exp-answered")
        accepted = await client.post(
            f"/api/v1/sessions/{answered['id']}/accept", headers=answered["mentor_headers"]
        )
        assert accepted.status_code == 200, accepted.text
        await past_deadline(db_engine, answered["id"])

        early = await fire(client, waiting["id"], kind=EXPIRY_KIND)
        late = await fire(client, answered["id"], kind=EXPIRY_KIND)

    assert early.status_code == 200 and late.status_code == 200
    assert early.json() == late.json() == {"queued": False}
    # Nothing written at all, not merely the status left alone.
    early_row = await row(db_engine, waiting["id"])
    late_row = await row(db_engine, answered["id"])
    assert (early_row["status"], early_row["events"], early_row["emails"]) == (
        "pending_mentor_approval",
        0,
        0,
    )
    assert (late_row["status"], late_row["events"], late_row["emails"]) == ("confirmed", 0, 0)


async def test_firing_twice_expires_once(db_engine: AsyncEngine) -> None:
    """QStash may deliver twice: one event, one refund, one email."""
    async with believing_client(db_engine) as client:
        request = await a_pending_request(db_engine, client, "exp-twice")
        await past_deadline(db_engine, request["id"])

        await fire(client, request["id"], kind=EXPIRY_KIND)
        await fire(client, request["id"], kind=EXPIRY_KIND)

    after = await row(db_engine, request["id"])
    assert (after["status"], after["events"], after["emails"]) == ("expired", 1, 2)


async def test_the_expiry_only_touches_its_own_request(db_engine: AsyncEngine) -> None:
    """**Scoped to the one session it names.** Another lapsed request is the
    sweep's, not this callback's, so nothing else moves early."""
    async with believing_client(db_engine) as client:
        named = await a_pending_request(db_engine, client, "exp-named")
        other = await a_pending_request(db_engine, client, "exp-other")
        await past_deadline(db_engine, named["id"])
        await past_deadline(db_engine, other["id"])

        await fire(client, named["id"], kind=EXPIRY_KIND)

    assert (await row(db_engine, named["id"]))["status"] == "expired"
    assert (await row(db_engine, other["id"]))["status"] == "pending_mentor_approval"


async def test_the_grace_is_short_and_positive() -> None:
    """Long enough that an early delivery still finds the deadline passed,
    short enough that nobody sees the request linger."""
    assert dt.timedelta(0) < EXPIRY_GRACE <= dt.timedelta(minutes=2)
