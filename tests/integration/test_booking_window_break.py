"""How far ahead an offering can be booked, and the break after each session.

Session Types frontend #13 (owner-approved 2026-09-28). Each is a mentor-level
default a session type inherits or overrides: `COALESCE(type, mentor, platform)`
— the same inherit pattern approval uses. The platform defaults are the 56-day
horizon and no break.

Slots are asked of `list_slots` directly with a fixed clock, because both rules
depend on it; booking legality and the next-free-time card go through the same
function, so they follow.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.integration.factories import add_availability, add_session_type, make_public_mentor
from tests.integration.test_api_me_session_type_writes import URL, as_mentor, body

from app.infra.db.slot_store import list_slots
from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]

#: A Monday, midnight UTC. Every weekday has 09:00-17:00 UTC hours below.
NOW = dt.datetime(2030, 1, 7, tzinfo=dt.UTC)


async def mentor_with_hours(engine: AsyncEngine, tag: str) -> UUID:
    mentor = await make_public_mentor(engine, tag, timezone="UTC")
    for day in range(7):
        await add_availability(
            engine, mentor, day_of_week=day, start="09:00", end="17:00", timezone="UTC"
        )
    return mentor


async def offering(
    engine: AsyncEngine,
    mentor: UUID,
    name: str,
    *,
    window: int | None = None,
    brk: int | None = None,
) -> UUID:
    session_type = await add_session_type(engine, mentor, name=name, duration=60, notice=1440)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE session_type_booking_configs SET booking_window_days = :w, "
                "break_after_minutes = :b WHERE session_type_id = :t"
            ),
            {"w": window, "b": brk, "t": session_type},
        )
    return session_type


async def mentor_defaults(
    engine: AsyncEngine, mentor: UUID, *, window: int | None, brk: int | None
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE mentor_profiles SET booking_window_days = :w, break_after_minutes = :b "
                "WHERE user_id = :u"
            ),
            {"w": window, "b": brk, "u": mentor},
        )


async def booked(engine: AsyncEngine, mentor: UUID, session_type: UUID, at: dt.datetime) -> None:
    async with engine.begin() as conn:
        mentee = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, first_name, primary_role, timezone) "
                    "VALUES (:e, 'Mo', 'mentee', 'UTC') RETURNING id"
                ),
                {"e": f"mentee-{uuid4()}@example.test"},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO sessions (mentor_id, mentee_id, session_type_id, starts_at, "
                "duration_minutes, status) VALUES (:m, :e, :t, :s, 60, 'confirmed')"
            ),
            {"m": mentor, "e": mentee, "t": session_type, "s": at},
        )


async def starts(
    engine: AsyncEngine, mentor: UUID, session_type: UUID, days: int = 56
) -> list[dt.datetime]:
    async with AsyncSession(engine) as session:
        slots = await list_slots(
            session,
            mentor,
            session_type,
            start=NOW.date(),
            end=NOW.date() + dt.timedelta(days=days),
            now=NOW,
        )
    assert slots is not None
    return [slot.start for slot in slots]


def at(day: int, hour: int) -> dt.datetime:
    return NOW + dt.timedelta(days=day, hours=hour)


# --------------------------------------------------------------------------
# The window
# --------------------------------------------------------------------------


async def test_a_type_is_bookable_only_within_its_window(db_engine: AsyncEngine) -> None:
    mentor = await mentor_with_hours(db_engine, "window-type")
    session_type = await offering(db_engine, mentor, "Short", window=7)

    found = await starts(db_engine, mentor, session_type)

    assert found
    assert max(found) < NOW + dt.timedelta(days=7)
    assert at(6, 9) in found  # inside the week


async def test_a_type_without_a_window_inherits_the_mentors(db_engine: AsyncEngine) -> None:
    mentor = await mentor_with_hours(db_engine, "window-inherit")
    await mentor_defaults(db_engine, mentor, window=14, brk=None)
    session_type = await offering(db_engine, mentor, "Inherits")

    found = await starts(db_engine, mentor, session_type)

    assert max(found) < NOW + dt.timedelta(days=14)
    assert at(13, 9) in found


async def test_with_no_window_anywhere_the_platform_horizon_applies(
    db_engine: AsyncEngine,
) -> None:
    mentor = await mentor_with_hours(db_engine, "window-platform")
    session_type = await offering(db_engine, mentor, "Default")

    found = await starts(db_engine, mentor, session_type)

    assert at(50, 9) in found


async def test_the_types_window_overrides_the_mentors(db_engine: AsyncEngine) -> None:
    mentor = await mentor_with_hours(db_engine, "window-override")
    await mentor_defaults(db_engine, mentor, window=7, brk=None)
    session_type = await offering(db_engine, mentor, "Longer", window=28)

    found = await starts(db_engine, mentor, session_type)

    assert at(20, 9) in found
    assert max(found) < NOW + dt.timedelta(days=28)


# --------------------------------------------------------------------------
# The break
# --------------------------------------------------------------------------


async def test_no_slot_starts_inside_a_booked_sessions_break(db_engine: AsyncEngine) -> None:
    mentor = await mentor_with_hours(db_engine, "break-after")
    session_type = await offering(db_engine, mentor, "Call", brk=30)
    await booked(db_engine, mentor, session_type, at(2, 10))  # 10:00-11:00, break to 11:30

    found = await starts(db_engine, mentor, session_type, days=7)

    assert at(2, 11) not in found
    assert at(2, 12) in found


async def test_a_new_sessions_own_break_must_fit_before_the_next(db_engine: AsyncEngine) -> None:
    """09:00-10:00 plus a 30-minute break runs into a 10:00 session."""
    mentor = await mentor_with_hours(db_engine, "break-before")
    session_type = await offering(db_engine, mentor, "Call", brk=30)
    await booked(db_engine, mentor, session_type, at(2, 10))

    found = await starts(db_engine, mentor, session_type, days=7)

    assert at(2, 9) not in found


async def test_a_break_applies_across_the_mentors_types(db_engine: AsyncEngine) -> None:
    """A mentor needs the break after any session, whichever offering is booked next."""
    mentor = await mentor_with_hours(db_engine, "break-across")
    long_break = await offering(db_engine, mentor, "Deep", brk=30)
    no_break = await offering(db_engine, mentor, "Quick", brk=0)
    await booked(db_engine, mentor, long_break, at(2, 10))

    found = await starts(db_engine, mentor, no_break, days=7)

    assert at(2, 11) not in found
    assert at(2, 9) in found  # the quick call's own break is zero


async def test_without_a_break_back_to_back_slots_stay(db_engine: AsyncEngine) -> None:
    mentor = await mentor_with_hours(db_engine, "break-none")
    session_type = await offering(db_engine, mentor, "Call")
    await booked(db_engine, mentor, session_type, at(2, 10))

    found = await starts(db_engine, mentor, session_type, days=7)

    assert at(2, 9) in found
    assert at(2, 11) in found


async def test_a_type_without_a_break_inherits_the_mentors(db_engine: AsyncEngine) -> None:
    mentor = await mentor_with_hours(db_engine, "break-inherit")
    await mentor_defaults(db_engine, mentor, window=None, brk=15)
    session_type = await offering(db_engine, mentor, "Call")
    await booked(db_engine, mentor, session_type, at(2, 10))

    found = await starts(db_engine, mentor, session_type, days=7)

    assert at(2, 11) not in found


# --------------------------------------------------------------------------
# Writing the rules
# --------------------------------------------------------------------------


async def test_an_offering_sets_and_clears_its_own_rules(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth = await as_mentor(db_engine, "rules-write")
    headers = bearer(api_token(auth))
    created = (
        await api_client.post(
            URL, json=body(booking_window_days=14, break_after_minutes=15), headers=headers
        )
    ).json()

    listed = (await api_client.get(URL, headers=headers)).json()["data"]
    (own,) = [t for t in listed if t["id"] == created["id"]]
    assert (own["booking_window_days"], own["break_after_minutes"]) == (14, 15)

    await api_client.patch(
        f"{URL}/{created['id']}", json={"booking_window_days": None}, headers=headers
    )
    listed = (await api_client.get(URL, headers=headers)).json()["data"]
    (own,) = [t for t in listed if t["id"] == created["id"]]
    assert (own["booking_window_days"], own["break_after_minutes"]) == (None, 15)


@pytest.mark.parametrize(
    "fields",
    [
        {"booking_window_days": 0},
        {"booking_window_days": 57},
        {"break_after_minutes": -5},
        {"break_after_minutes": 121},
    ],
)
async def test_rules_outside_the_range_are_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fields: dict[str, int]
) -> None:
    _, auth = await as_mentor(db_engine, f"rules-range-{uuid4().hex[:6]}")

    response = await api_client.post(URL, json=body(**fields), headers=bearer(api_token(auth)))

    assert response.status_code == 422


async def test_a_mentor_sets_their_defaults(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "rules-defaults")
    headers = bearer(api_token(auth))
    path = f"/api/v1/users/{mentor}/mentor-profile"

    response = await api_client.patch(
        path, json={"booking_window_days": 28, "break_after_minutes": 10}, headers=headers
    )
    profile = (await api_client.get(path, headers=headers)).json()

    assert response.status_code in (200, 204)
    assert (profile["booking_window_days"], profile["break_after_minutes"]) == (28, 10)
