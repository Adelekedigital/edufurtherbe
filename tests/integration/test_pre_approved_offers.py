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
    open_slots,
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
    # A booking, not a request waiting on the mentor; both parties are told.
    assert await told(db_engine, booked.json()["id"]) == ["session_booked"] * 2
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


async def test_past_the_hold_the_mentors_notice_applies_again(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The approval outlasts the hold; the notice measured from the offer does
    not.** While held, notice is measured from when the time was offered, so a
    time valid then stays bookable. Past the hold the time must be one the grid
    offers now, or a lapsed offer would let a mentee confirm a session minutes
    before it starts, with no approval and inside the mentor's notice."""
    booking = await a_booking(db_engine, api_client, "pa-notice")
    session_type = await offering_of(api_client, booking)
    soon = next(s for s in await open_slots(api_client, booking) if s != booking["starts_at"])
    await end_with_suggestion(api_client, booking, "decline", soon)
    await lapse(db_engine, booking)
    async with db_engine.begin() as conn:
        # Offered a month ago, with a month's notice now: measured from the
        # offer the time would pass, measured from now it cannot.
        await conn.execute(
            text(
                "UPDATE session_suggestions SET created_at = now() - interval '31 days' "
                "WHERE session_id = :i"
            ),
            {"i": booking["id"]},
        )
        changed = await conn.execute(
            text(
                "UPDATE session_type_booking_configs SET min_notice_minutes = 43200 "
                "WHERE session_type_id = :t"
            ),
            {"t": session_type},
        )
        assert changed.rowcount == 1

    refused = await book(api_client, session_type, soon, booking["mentee"])

    assert refused.status_code == 422, refused.text
    assert (await suggestion_of(api_client, booking))["booked_session_id"] is None


async def test_a_decline_and_a_cancel_offering_one_time_book_it_once_confirmed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Two offers of one time to one mentee**: a decline's, lapsed, and a later
    cancel's, held. Booking it honours the hold (no conflict with their own
    offer), confirms because one of them was approved in advance, and spends
    both, so neither can be booked again."""
    first = await a_booking(db_engine, api_client, "pa-two")
    session_type = await offering_of(api_client, first)
    at = await another_slot(api_client, first)
    await end_with_suggestion(api_client, first, "decline", at)
    await lapse(db_engine, first)
    slots = [s for s in await open_slots(api_client, first) if s not in (at, first["starts_at"])]
    second = await book(api_client, session_type, slots[-1], first["mentee"])
    assert second.status_code == 201, second.text
    accepted = await api_client.post(
        f"/api/v1/sessions/{second.json()['id']}/accept", headers=first["mentor"]
    )
    assert accepted.status_code == 200, accepted.text
    again = {**first, "id": second.json()["id"]}
    cancelled = await end_with_suggestion(api_client, again, "cancel", at)
    assert cancelled.status_code == 200, cancelled.text

    booked = await book(api_client, session_type, at, first["mentee"])

    assert booked.status_code == 201, booked.text
    assert booked.json()["status"] == "confirmed"
    assert (await suggestion_of(api_client, first))["status"] == "booked"
    assert (await suggestion_of(api_client, again))["status"] == "booked"
