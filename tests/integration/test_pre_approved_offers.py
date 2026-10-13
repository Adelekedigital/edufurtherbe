"""A time offered on a decline is approved in advance (owner, 2026-10-13).

A mentor who declines a pending request and suggests another time picked that
time from their own hours for this one mentee, so booking it confirms at once
instead of waiting on them a second time. Every case the rule draws a line at
has a test here: the decline's offer, held or lapsed; a cancel's offer, which
was never a pending request; and anyone but the mentee it was offered to.

Each mentor here **requires approval** (`a_booking` sets it on an unconfirmed
booking), so a booking that confirms did so because of the offer, not because
the offering confirms everyone.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_booking import a_mentee
from tests.integration.test_api_session_suggestions import (
    another_slot,
    book,
    end_with_suggestion,
    lapse,
    offering_of,
    suggestion_of,
)
from tests.integration.test_api_session_transitions import a_booking

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def status_of(client: httpx.AsyncClient, session_id: str, headers: dict[str, str]) -> str:
    read = await client.get(f"/api/v1/sessions/{session_id}", headers=headers)
    assert read.status_code == 200, read.text
    return str(read.json()["status"])


async def told(engine: AsyncEngine, session_id: str) -> list[str]:
    """What the booking queued about itself."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT event_type FROM outbox_events WHERE entity_id = :i ORDER BY created_at"),
            {"i": session_id},
        )
        return [str(row[0]) for row in rows]


async def require_approval(engine: AsyncEngine, booking: dict[str, Any]) -> None:
    """The mentor of a confirmed booking turns approval on afterwards."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE mentor_profiles SET requires_booking_confirmation = true WHERE user_id = :m"
            ),
            {"m": booking["mentor_id"]},
        )


async def test_booking_a_declines_offer_confirms_at_once(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(db_engine, api_client, "pa-held")
    session_type = await offering_of(api_client, booking)
    at = await another_slot(api_client, booking)
    await end_with_suggestion(api_client, booking, "decline", at)

    booked = await book(api_client, session_type, at, booking["mentee"])

    assert booked.status_code == 201, booked.text
    assert booked.json()["status"] == "confirmed"
    assert booked.json()["respond_by"] is None
    # The mentor learns of a booking, not of a request waiting on them.
    assert await told(db_engine, booked.json()["id"]) == ["session_booked"]
    assert (await suggestion_of(api_client, booking))["status"] == "booked"


async def test_the_approval_outlasts_the_hold(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The hold only hides the time from others**; the approval lasts while
    the offer is unbooked and the time is still free."""
    booking = await a_booking(db_engine, api_client, "pa-lapsed")
    session_type = await offering_of(api_client, booking)
    at = await another_slot(api_client, booking)
    await end_with_suggestion(api_client, booking, "decline", at)
    await lapse(db_engine, booking)

    booked = await book(api_client, session_type, at, booking["mentee"])

    assert booked.status_code == 201, booked.text
    assert booked.json()["status"] == "confirmed"
    suggestion = await suggestion_of(api_client, booking)
    assert suggestion["status"] == "booked"
    assert suggestion["booked_session_id"] == booked.json()["id"]


async def test_a_lapsed_declines_offer_is_still_booked_once(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(db_engine, api_client, "pa-once")
    session_type = await offering_of(api_client, booking)
    at = await another_slot(api_client, booking)
    await end_with_suggestion(api_client, booking, "decline", at)
    await lapse(db_engine, booking)
    await book(api_client, session_type, at, booking["mentee"])

    again = await book(api_client, session_type, at, booking["mentee"])

    assert again.status_code == 422, again.text


async def test_anyone_else_booking_that_hour_still_waits(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The approval is for that mentee at that time**, so once the hold lapses
    and the hour is public, a stranger books it as an ordinary request, and the
    mentee's offer stays theirs."""
    booking = await a_booking(db_engine, api_client, "pa-stranger")
    session_type = await offering_of(api_client, booking)
    at = await another_slot(api_client, booking)
    await end_with_suggestion(api_client, booking, "decline", at)
    await lapse(db_engine, booking)
    _, stranger = await a_mentee(db_engine, "pa-stranger-b")

    booked = await book(api_client, session_type, at, stranger)

    assert booked.status_code == 201, booked.text
    assert booked.json()["status"] == "pending_mentor_approval"
    assert (await suggestion_of(api_client, booking))["booked_session_id"] is None


async def test_a_cancels_offer_waits_for_approval_as_before(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Only a decline approves in advance** (owner, 2026-10-13): a cancel ends
    a session that was already agreed, so there was no pending request whose
    approval the offer could stand in for."""
    booking = await a_booking(db_engine, api_client, "pa-cancel", confirmed=True)
    session_type = await offering_of(api_client, booking)
    at = await another_slot(api_client, booking)
    await end_with_suggestion(api_client, booking, "cancel", at)
    await require_approval(db_engine, booking)

    booked = await book(api_client, session_type, at, booking["mentee"])

    assert booked.status_code == 201, booked.text
    assert booked.json()["status"] == "pending_mentor_approval"
    assert await told(db_engine, booked.json()["id"]) == ["session_requested"]


async def test_a_cancels_lapsed_offer_is_not_taken_up(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Past its hold, a cancel's offer is spent by nothing: the time books as an
    ordinary request, exactly as before this rule."""
    booking = await a_booking(db_engine, api_client, "pa-cancel-lapsed", confirmed=True)
    session_type = await offering_of(api_client, booking)
    at = await another_slot(api_client, booking)
    await end_with_suggestion(api_client, booking, "cancel", at)
    await require_approval(db_engine, booking)
    await lapse(db_engine, booking)

    booked = await book(api_client, session_type, at, booking["mentee"])

    assert booked.status_code == 201, booked.text
    assert booked.json()["status"] == "pending_mentor_approval"
    assert (await suggestion_of(api_client, booking))["status"] == "expired"
