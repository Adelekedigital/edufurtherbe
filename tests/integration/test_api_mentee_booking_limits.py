"""The mentee's booking limits (#342, settled decision 231).

Three refusals, each with its own problem type, and an accepting case beside
each: a limit that refused everything would pass every rejecting test.

Every session here is booked through the API from a real `/slots` instant, as
`test_api_booking` does, except where a test needs a session in a state the API
cannot reach on demand (completed, or at a chosen time); those are set with SQL
on a row the API wrote.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.factories import until_blocked
from tests.integration.test_api_booking import (
    URL,
    a_bookable_offering,
    a_mentee,
    body,
    key,
)

from app.core.errors import BookingLimitReachedError
from app.domain.booking_limits import MAX_LIVE_MENTEE_SESSIONS
from app.infra.db.mentee_limits import check_mentee_limits

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

OVERLAP = "/problems/booking-overlap"
WITH_MENTOR = "/problems/booking-with-mentor-exists"
LIMIT = "/problems/booking-limit-reached"


async def slots(client: httpx.AsyncClient, mentor: UUID, session_type: UUID) -> list[str]:
    response = await client.get(
        f"/api/v1/users/{mentor}/availability/slots",
        params={"session_type_id": str(session_type)},
    )
    assert response.status_code == 200, response.text
    return [str(s["start"]) for s in response.json()["data"]]


async def book(
    client: httpx.AsyncClient, headers: dict[str, str], session_type: UUID, starts_at: str
) -> httpx.Response:
    return await client.post(URL, json=body(session_type, starts_at), headers=headers | key())


async def set_status(engine: AsyncEngine, session_id: str, status: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET status = :s WHERE id = :i"), {"s": status, "i": session_id}
        )


# --------------------------------------------------------------------------
# One live session per mentor
# --------------------------------------------------------------------------


async def test_a_second_live_session_with_the_same_mentor_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, session_type = await a_bookable_offering(db_engine, "lim-pair")
    _, headers = await a_mentee(db_engine, "lim-pair")
    times = await slots(api_client, mentor, session_type)

    first = await book(api_client, headers, session_type, times[0])
    second = await book(api_client, headers, session_type, times[-1])

    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text
    assert second.json()["type"] == WITH_MENTOR


@pytest.mark.parametrize("ended", ["completed", "cancelled", "declined", "expired", "withdrawn"])
async def test_the_same_mentor_can_be_booked_again_once_the_session_is_over(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, ended: str
) -> None:
    """A session leaves every count once it is no longer live, whatever ended it."""
    mentor, session_type = await a_bookable_offering(db_engine, f"lim-again-{ended}")
    _, headers = await a_mentee(db_engine, f"lim-again-{ended}")
    times = await slots(api_client, mentor, session_type)
    first = await book(api_client, headers, session_type, times[0])
    await set_status(db_engine, first.json()["id"], ended)

    again = await book(api_client, headers, session_type, times[-1])

    assert again.status_code == 201, again.text


# --------------------------------------------------------------------------
# The overall cap
# --------------------------------------------------------------------------


async def test_two_live_sessions_with_different_mentors_are_allowed_and_a_third_is_not(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The cap's boundary from both sides: the second is fine, the third is not."""
    assert MAX_LIVE_MENTEE_SESSIONS == 2
    _, headers = await a_mentee(db_engine, "lim-cap")
    offerings = [await a_bookable_offering(db_engine, f"lim-cap-{n}") for n in range(3)]
    picks = [await slots(api_client, m, t) for m, t in offerings]

    # Three times far apart, so no overlap explains a refusal.
    responses = [
        await book(api_client, headers, offerings[n][1], picks[n][n * 4]) for n in range(3)
    ]

    assert [r.status_code for r in responses[:2]] == [201, 201], [r.text for r in responses]
    assert responses[2].status_code == 409, responses[2].text
    assert responses[2].json()["type"] == LIMIT


async def test_a_session_that_ends_frees_a_place_under_the_cap(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, headers = await a_mentee(db_engine, "lim-free")
    offerings = [await a_bookable_offering(db_engine, f"lim-free-{n}") for n in range(3)]
    picks = [await slots(api_client, m, t) for m, t in offerings]
    first = await book(api_client, headers, offerings[0][1], picks[0][0])
    await book(api_client, headers, offerings[1][1], picks[1][4])
    await set_status(db_engine, first.json()["id"], "completed")

    third = await book(api_client, headers, offerings[2][1], picks[2][8])

    assert third.status_code == 201, third.text


# --------------------------------------------------------------------------
# No overlap
# --------------------------------------------------------------------------


async def test_an_overlapping_time_with_another_mentor_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Both offerings are hourly from midnight, so their first slots coincide."""
    _, headers = await a_mentee(db_engine, "lim-overlap")
    (m1, t1), (m2, t2) = [await a_bookable_offering(db_engine, f"lim-ov-{n}") for n in range(2)]
    shared = sorted(set(await slots(api_client, m1, t1)) & set(await slots(api_client, m2, t2)))

    first = await book(api_client, headers, t1, shared[0])
    second = await book(api_client, headers, t2, shared[0])

    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text
    assert second.json()["type"] == OVERLAP


async def test_back_to_back_sessions_with_two_mentors_are_allowed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Touching is not overlapping: one ends at 10:00, the next starts at 10:00."""
    _, headers = await a_mentee(db_engine, "lim-touch")
    (m1, t1), (m2, t2) = [await a_bookable_offering(db_engine, f"lim-tc-{n}") for n in range(2)]
    # The first pair that actually touches. Taking the first slot and its
    # successor failed whenever the run's clock made that slot the last of its
    # day's window, so the pair is searched for rather than assumed.
    second_times = {dt.datetime.fromisoformat(s): s for s in await slots(api_client, m2, t2)}
    before, after = next(
        (s, second_times[dt.datetime.fromisoformat(s) + dt.timedelta(minutes=60)])
        for s in await slots(api_client, m1, t1)
        if dt.datetime.fromisoformat(s) + dt.timedelta(minutes=60) in second_times
    )

    first = await book(api_client, headers, t1, before)
    second = await book(api_client, headers, t2, after)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text


async def test_the_database_refuses_an_overlap_written_around_the_check(
    db_engine: AsyncEngine,
) -> None:
    """The wall a race cannot get past: two overlapping live rows for one mentee,
    inserted directly, are refused by `sessions_no_mentee_double_booking`."""
    (m1, t1), (m2, t2) = [await a_bookable_offering(db_engine, f"lim-db-{n}") for n in range(2)]
    mentee, _ = await a_mentee(db_engine, "lim-db")
    at = dt.datetime.now(dt.UTC) + dt.timedelta(days=3)
    insert = text(
        "INSERT INTO sessions (mentor_id, mentee_id, session_type_id, starts_at, "
        "duration_minutes, status) VALUES (:m, :e, :t, :s, 60, 'confirmed')"
    )
    async with db_engine.begin() as conn:
        await conn.execute(insert, {"m": m1, "e": mentee, "t": t1, "s": at})

    with pytest.raises(IntegrityError, match="sessions_no_mentee_double_booking"):
        async with db_engine.begin() as conn:
            await conn.execute(
                insert, {"m": m2, "e": mentee, "t": t2, "s": at + dt.timedelta(minutes=30)}
            )


async def test_an_ended_session_does_not_block_the_same_time(db_engine: AsyncEngine) -> None:
    """The constraint covers live statuses only: a cancelled session at the same
    time as a live one is an ordinary history, not a double booking."""
    (m1, t1), (m2, t2) = [await a_bookable_offering(db_engine, f"lim-dbc-{n}") for n in range(2)]
    mentee, _ = await a_mentee(db_engine, "lim-dbc")
    at = dt.datetime.now(dt.UTC) + dt.timedelta(days=3)
    insert = text(
        "INSERT INTO sessions (mentor_id, mentee_id, session_type_id, starts_at, "
        "duration_minutes, status) VALUES (:m, :e, :t, :s, 60, :st)"
    )
    async with db_engine.begin() as conn:
        await conn.execute(insert, {"m": m1, "e": mentee, "t": t1, "s": at, "st": "cancelled"})
        await conn.execute(insert, {"m": m2, "e": mentee, "t": t2, "s": at, "st": "confirmed"})


# --------------------------------------------------------------------------
# Racing
# --------------------------------------------------------------------------


async def test_two_bookings_racing_past_the_cap_are_serialised(db_engine: AsyncEngine) -> None:
    """The lock is what holds the count. The mentee already has one live session;
    two transactions each check for a second. Without the lock both would see
    one, both pass, and both insert. With it the second waits, then sees two."""
    (m1, t1), (m2, t2), (m3, _) = [
        await a_bookable_offering(db_engine, f"lim-race-{n}") for n in range(3)
    ]
    mentee, _ = await a_mentee(db_engine, "lim-race")
    base = dt.datetime.now(dt.UTC).replace(microsecond=0) + dt.timedelta(days=3)
    insert = text(
        "INSERT INTO sessions (mentor_id, mentee_id, session_type_id, starts_at, "
        "duration_minutes, status) VALUES (:m, :e, :t, :s, 60, 'confirmed')"
    )
    async with db_engine.begin() as conn:
        await conn.execute(insert, {"m": m1, "e": mentee, "t": t1, "s": base})

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    first = factory()
    second = factory()
    try:
        await check_mentee_limits(
            first, mentee, mentor_id=m2, starts_at=base + dt.timedelta(hours=4), duration_minutes=60
        )
        await first.execute(
            insert, {"m": m2, "e": mentee, "t": t2, "s": base + dt.timedelta(hours=4)}
        )
        racer = asyncio.create_task(
            check_mentee_limits(
                second,
                mentee,
                mentor_id=m3,
                starts_at=base + dt.timedelta(hours=8),
                duration_minutes=60,
            )
        )
        await until_blocked(db_engine)
        await first.commit()

        with pytest.raises(BookingLimitReachedError):
            await racer
    finally:
        await first.close()
        await second.close()
