"""The booking window is configuration (Round 5, owner 2026-09-29).

`MAX_BOOKING_WINDOW_DAYS` bounds what any offering or mentor may set and clamps
what they already stored — on read, never rewritten, so raising it back restores
their choice. `DEFAULT_BOOKING_WINDOW_DAYS` is what an offering gets when neither
it nor its mentor sets one. Slots, booking legality and the reads all resolve
through `booking_rules.effective_window_days`, so they cannot disagree.
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tests.integration.test_api_booking import a_mentee
from tests.integration.test_api_me_session_type_writes import URL, as_mentor, body
from tests.integration.test_booking_window_break import (
    NOW,
    at,
    mentor_defaults,
    mentor_with_hours,
    offering,
)

from app.core.config import Settings
from app.core.errors import ValidationError
from app.domain.availability import BookingWindow
from app.infra.db.session_writer import book_session
from app.infra.db.slot_store import list_slots
from app.infra.storage.supabase import SupabaseStorage
from conftest import PLATFORM_WINDOW, api_token, bearer, build_api_app, client_for

pytestmark = [pytest.mark.db, pytest.mark.anyio]

#: A deployment that lowered the maximum to a fortnight.
FORTNIGHT = BookingWindow(max_days=14, default_days=14)


async def starts(
    engine: AsyncEngine, mentor: object, session_type: object, window: BookingWindow, days: int
) -> list[dt.datetime]:
    async with AsyncSession(engine) as session:
        slots = await list_slots(
            session,
            mentor,  # type: ignore[arg-type]
            session_type,  # type: ignore[arg-type]
            start=NOW.date(),
            end=NOW.date() + dt.timedelta(days=days),
            now=NOW,
            window=window,
        )
    assert slots is not None
    return [slot.start for slot in slots]


def fortnight_client(db_engine: AsyncEngine, storage: SupabaseStorage | None) -> httpx.AsyncClient:
    settings = Settings(_env_file=None, max_booking_window_days=14, default_booking_window_days=14)
    return client_for(build_api_app(db_engine, storage, settings))


# --------------------------------------------------------------------------
# Clamped on read, restored when the maximum comes back
# --------------------------------------------------------------------------


async def test_a_stored_window_above_a_lowered_max_is_clamped(db_engine: AsyncEngine) -> None:
    mentor = await mentor_with_hours(db_engine, "clamp-type")
    session_type = await offering(db_engine, mentor, "Long", window=56)

    found = await starts(db_engine, mentor, session_type, FORTNIGHT, days=14)

    assert at(13, 9) in found
    assert max(found) < NOW + dt.timedelta(days=14)


async def test_raising_the_max_back_restores_the_stored_window(db_engine: AsyncEngine) -> None:
    mentor = await mentor_with_hours(db_engine, "clamp-restore")
    session_type = await offering(db_engine, mentor, "Long", window=56)

    await starts(db_engine, mentor, session_type, FORTNIGHT, days=14)
    found = await starts(db_engine, mentor, session_type, PLATFORM_WINDOW, days=56)

    assert at(50, 9) in found


async def test_a_mentor_default_above_the_max_is_clamped_too(db_engine: AsyncEngine) -> None:
    mentor = await mentor_with_hours(db_engine, "clamp-mentor")
    await mentor_defaults(db_engine, mentor, window=40, brk=None)
    session_type = await offering(db_engine, mentor, "Inherits")

    found = await starts(db_engine, mentor, session_type, FORTNIGHT, days=14)

    assert max(found) < NOW + dt.timedelta(days=14)


async def test_the_default_window_applies_when_nothing_sets_one(db_engine: AsyncEngine) -> None:
    mentor = await mentor_with_hours(db_engine, "default-window")
    session_type = await offering(db_engine, mentor, "Default")
    week = BookingWindow(max_days=56, default_days=7)

    found = await starts(db_engine, mentor, session_type, week, days=14)

    assert at(6, 9) in found
    assert max(found) < NOW + dt.timedelta(days=7)


async def test_a_slots_range_wider_than_the_max_is_refused(db_engine: AsyncEngine) -> None:
    mentor = await mentor_with_hours(db_engine, "range-cap")
    session_type = await offering(db_engine, mentor, "Any")

    with pytest.raises(ValidationError, match="at most 14 days"):
        await starts(db_engine, mentor, session_type, FORTNIGHT, days=15)


async def test_booking_beyond_a_lowered_max_is_refused(db_engine: AsyncEngine) -> None:
    """The legality check asks the same grid, so a clamped window refuses the
    booking the stored one would have allowed."""
    mentor = await mentor_with_hours(db_engine, "clamp-book")
    session_type = await offering(db_engine, mentor, "Long", window=56)
    mentee, _ = await a_mentee(db_engine, "clamp-book")
    payload = {"session_type_id": session_type, "starts_at": at(20, 9)}
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session:
        with pytest.raises(ValidationError, match="not available"):
            await book_session(
                session, mentee, payload, now=NOW, require_answers=False, window=FORTNIGHT
            )
        await session.rollback()
        await book_session(
            session, mentee, payload, now=NOW, require_answers=False, window=PLATFORM_WINDOW
        )


# --------------------------------------------------------------------------
# Writes are bounded by the configured maximum
# --------------------------------------------------------------------------


async def test_a_session_type_window_over_the_max_is_refused_by_name(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    _, auth = await as_mentor(db_engine, "write-over-max")
    headers = bearer(api_token(auth))

    async with fortnight_client(db_engine, api_storage) as client:
        created = await client.post(URL, json=body(booking_window_days=30), headers=headers)
        allowed = await client.post(
            URL, json=body(name="Fits", booking_window_days=14), headers=headers
        )
        patched = await client.patch(
            f"{URL}/{allowed.json()['id']}", json={"booking_window_days": 15}, headers=headers
        )

    assert created.status_code == 422, created.text
    assert created.json()["errors"][0]["pointer"] == "/booking_window_days"
    assert allowed.status_code == 201, allowed.text
    assert patched.status_code == 422, patched.text
    assert patched.json()["errors"][0]["pointer"] == "/booking_window_days"


async def test_a_mentor_default_window_over_the_max_is_refused_by_name(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    mentor, auth = await as_mentor(db_engine, "profile-over-max")
    headers = bearer(api_token(auth))
    url = f"/api/v1/users/{mentor}/mentor-profile"

    async with fortnight_client(db_engine, api_storage) as client:
        refused = await client.patch(url, json={"booking_window_days": 30}, headers=headers)
        allowed = await client.patch(url, json={"booking_window_days": 14}, headers=headers)

    assert refused.status_code == 422, refused.text
    assert refused.json()["errors"][0]["pointer"] == "/booking_window_days"
    assert allowed.status_code == 200, allowed.text


# --------------------------------------------------------------------------
# The reads carry what the frontend builds its options and modal from
# --------------------------------------------------------------------------


async def test_the_reads_carry_the_resolved_window_and_the_platform_bounds(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    mentor, auth = await as_mentor(db_engine, "reads-window")
    headers = bearer(api_token(auth))
    session_type = await offering(db_engine, mentor, "Long", window=56)

    async with fortnight_client(db_engine, api_storage) as client:
        public = (await client.get(f"/api/v1/users/{mentor}/session-types")).json()["data"]
        own = (await client.get(URL, headers=headers)).json()["data"]
        profile = (
            await client.get(f"/api/v1/users/{mentor}/mentor-profile", headers=headers)
        ).json()

    (listed,) = [t for t in public if t["id"] == str(session_type)]
    (mine,) = [t for t in own if t["id"] == str(session_type)]
    assert listed["booking_window_days"] == 14
    assert (mine["booking_window_days"], mine["effective_booking_window_days"]) == (56, 14)
    assert (profile["max_booking_window_days"], profile["default_booking_window_days"]) == (14, 14)


async def test_with_the_defaults_nothing_changes(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "reads-default")
    session_type = await offering(db_engine, mentor, "Plain")

    public = (await api_client.get(f"/api/v1/users/{mentor}/session-types")).json()["data"]
    profile = (
        await api_client.get(
            f"/api/v1/users/{mentor}/mentor-profile", headers=bearer(api_token(auth))
        )
    ).json()

    (listed,) = [t for t in public if t["id"] == str(session_type)]
    assert listed["booking_window_days"] == PLATFORM_WINDOW.default_days
    assert profile["max_booking_window_days"] == PLATFORM_WINDOW.max_days
