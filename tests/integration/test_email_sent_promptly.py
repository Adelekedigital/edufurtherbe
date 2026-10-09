"""Email is sent when the request that owes it finishes, not at the hourly sweep.

The outbox was drained only by the `settle-sessions` job (`30 * * * *`), so a
booking confirmation, an accept or a decline could reach its recipient up to an
hour late. Owner, 2026-10-09: urgent. Each request now sends what it queued once
its response has gone; the hourly sweep is left to retry what failed.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_notifications import a_booking, queued

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


class Recorder:
    """A notifier that records what it was asked to send."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, **kwargs: Any) -> None:
        self.sent.append(kwargs)


async def test_a_booking_s_email_is_sent_by_the_booking_itself(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Sent during the request, not an hour later.** Every row the booking queued
    is `sent` by the time the call returns."""
    recorder = Recorder()
    api_client._transport.app.state.notifier = recorder  # type: ignore[attr-defined]

    booking = await a_booking(db_engine, api_client, "prompt-book")

    rows = await queued(db_engine, booking["id"])
    assert rows, "the booking queues email"
    assert {row["status"] for row in rows} == {"sent"}
    assert len(recorder.sent) == len(rows)


async def test_a_request_sends_only_what_it_queued(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Only its own rows.** A message another request left pending, a retry
    the sweep owns, is not swept up by an unrelated booking."""
    first = await a_booking(db_engine, api_client, "prompt-first")
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE outbox_events SET status = 'pending', sent_at = NULL WHERE entity_id = :i"
            ),
            {"i": first["id"]},
        )
    recorder = Recorder()
    api_client._transport.app.state.notifier = recorder  # type: ignore[attr-defined]

    await a_booking(db_engine, api_client, "prompt-second")

    assert {row["status"] for row in await queued(db_engine, first["id"])} == {"pending"}
