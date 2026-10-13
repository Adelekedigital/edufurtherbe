"""An offered time survives the mentor changing the offering's length (#347).

The grid steps by the offering's *current* length from each window's start, so
a 60-minute offering offers every hour and a 45-minute one offers 00:00, 00:45,
01:30 and so on. An hour offered while the offering ran 60 minutes is then off
the grid, though nothing about the hour changed. An offer is booked at the length
it was offered at (decision 230), so it is looked for on the grid drawn at that
length, where it was when it was offered; everything else about the grid (hours,
busy times, notice) is the mentor's current one.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import LAGOS
from tests.integration.test_api_session_suggestions import (
    book,
    end_with_suggestion,
    lapse,
    offering_of,
    open_slots,
    suggestion_of,
)
from tests.integration.test_api_session_transitions import a_booking

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def off_the_45_minute_grid(client: httpx.AsyncClient, booking: dict[str, Any]) -> str:
    """An hour the 60-minute grid offers and a 45-minute one never will: the
    windows start at 00:00 in the mentor's zone, so 45-minute steps land on the
    hour only every third hour there."""
    return next(
        s
        for s in reversed(await open_slots(client, booking))
        if s != booking["starts_at"]
        and dt.datetime.fromisoformat(s).astimezone(ZoneInfo(LAGOS)).hour % 3
    )


async def shorten_to(engine: AsyncEngine, session_type: str, minutes: int) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE session_type_booking_configs SET duration_minutes = :m "
                "WHERE session_type_id = :t"
            ),
            {"m": minutes, "t": session_type},
        )


async def test_a_held_offer_is_bookable_after_the_length_changes(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(db_engine, api_client, "len-held")
    session_type = await offering_of(api_client, booking)
    at = await off_the_45_minute_grid(api_client, booking)
    await end_with_suggestion(api_client, booking, "decline", at)
    await shorten_to(db_engine, session_type, 45)

    booked = await book(api_client, session_type, at, booking["mentee"])

    assert booked.status_code == 201, booked.text
    assert booked.json()["duration_minutes"] == 60
    assert (await suggestion_of(api_client, booking))["status"] == "booked"


async def test_an_approved_offer_past_its_hold_survives_the_change_too(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A decline's offer stays takeable past the hold (decision 244), so the
    same check applies there: the hour is still free, so it is still bookable."""
    booking = await a_booking(db_engine, api_client, "len-lapsed")
    session_type = await offering_of(api_client, booking)
    at = await off_the_45_minute_grid(api_client, booking)
    await end_with_suggestion(api_client, booking, "decline", at)
    await lapse(db_engine, booking)
    await shorten_to(db_engine, session_type, 45)
    # The premise, checked rather than assumed: past the hold the time is public,
    # and the 45-minute grid really does not offer it.
    assert at not in await open_slots(api_client, booking)

    booked = await book(api_client, session_type, at, booking["mentee"])

    assert booked.status_code == 201, booked.text
    assert booked.json()["status"] == "confirmed"
    assert booked.json()["duration_minutes"] == 60


async def test_an_offer_outside_the_mentors_hours_is_still_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The negative case, on the same path**: the length changed too, so the
    grid is drawn at the offer's length, and only the mentor's hours refuse it.
    A mentor who no longer declares that hour has withdrawn it."""
    booking = await a_booking(db_engine, api_client, "len-outside")
    session_type = await offering_of(api_client, booking)
    at = await off_the_45_minute_grid(api_client, booking)
    await end_with_suggestion(api_client, booking, "decline", at)
    await shorten_to(db_engine, session_type, 45)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE availability_rules SET end_time = '01:00' WHERE mentor_user_id = :m"),
            {"m": booking["mentor_id"]},
        )

    refused = await book(api_client, session_type, at, booking["mentee"])

    assert refused.status_code == 422, refused.text
    assert (await suggestion_of(api_client, booking))["booked_session_id"] is None
