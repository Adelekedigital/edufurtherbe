"""Per-offering scheduling windows, which replace general availability.

**The regression is the point of this file, not the feature.** `slot_store` is
built and tested, and every existing slot test assumes `availability_rules` is
the only source. So the first test asserts that an offering with **no** windows
produces exactly what it produced before — that is what keeps a tested read path
tested, and it covers the entire current population, because no offering has a
window.

The feature tests are the rest: windows replace rather than intersect, exceptions
still subtract, and a mentor with windows but no general availability is bookable
rather than misconfigured.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import (
    add_availability,
    add_block,
    add_scheduling_window,
    add_session_type,
    make_public_mentor,
)

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

#: Far enough ahead that no notice window reaches it, and **relative** so the
#: suite does not rot. Every fixture below seeds all seven weekdays for the same
#: reason the existing slot suite does: a test that depends on which day it runs
#: on is a test that fails on a Tuesday (#99).
DAY = dt.date.today() + dt.timedelta(days=7)


async def seed_availability(engine: AsyncEngine, mentor: UUID, start: str, end: str) -> None:
    for day_of_week in range(7):
        await add_availability(engine, mentor, day_of_week=day_of_week, start=start, end=end)


async def seed_windows(
    engine: AsyncEngine, session_type: UUID, start: str, end: str, *, active: bool = True
) -> None:
    for day_of_week in range(7):
        await add_scheduling_window(
            engine, session_type, day_of_week=day_of_week, start=start, end=end, active=active
        )


async def starts(client: httpx.AsyncClient, mentor: UUID, session_type: UUID) -> list[str]:
    response = await client.get(
        f"/api/v1/users/{mentor}/availability/slots",
        params={
            "session_type_id": str(session_type),
            "start": DAY.isoformat(),
            "end": (DAY + dt.timedelta(days=1)).isoformat(),
        },
    )
    assert response.status_code == 200, response.text
    return [slot["start"] for slot in response.json()["data"]]


async def test_an_offering_with_no_windows_is_unchanged(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The regression test, and it is not a feature test.**

    Every offering in existence has no windows, so this covers the entire current
    population. Had windows been made to *intersect* general availability rather
    than replace it, an offering with none would intersect with nothing and
    return an empty list — every slot in the product gone, while the feature
    tests below still passed.

    Two offerings on one mentor, only one of which has a window, so this also
    catches a query that read windows without scoping them to the offering.
    """
    mentor = await make_public_mentor(db_engine, "windows-none")
    plain = await add_session_type(db_engine, mentor, name="Plain", duration=60)
    windowed = await add_session_type(db_engine, mentor, name="Windowed", duration=60)
    await seed_availability(db_engine, mentor, "09:00", "12:00")
    await seed_windows(db_engine, windowed, "17:00", "19:00")

    plain_slots = await starts(api_client, mentor, plain)

    assert plain_slots, "the mentor's general availability stopped producing slots"
    assert len(plain_slots) == 3


async def test_windows_replace_general_availability_rather_than_intersecting(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The mock's own example, and why intersecting is wrong.**

    Working hours in the morning, a deliberate evening window. Intersected they
    share nothing, and the mentor sees an empty calendar with nothing to explain
    it. Replaced, the evening window is exactly what they asked for.

    Asserted as a count and a disjointness rather than as literal instants: the
    fixture's zone is Lagos and the point is *which window won*, not the offset.
    """
    mentor = await make_public_mentor(db_engine, "windows-replace")
    offering = await add_session_type(db_engine, mentor, duration=60)
    await seed_availability(db_engine, mentor, "09:00", "12:00")
    await seed_windows(db_engine, offering, "17:00", "19:00")

    slots = await starts(api_client, mentor, offering)

    assert len(slots) == 2, "the evening window did not replace the morning availability"


async def test_a_mentor_with_windows_and_no_general_availability_is_bookable(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Newly reachable, and **not** a misconfiguration.

    Before windows, no availability rules meant no slots, full stop. The decision
    names this state explicitly so nobody later "fixes" it by requiring general
    availability as a precondition.
    """
    mentor = await make_public_mentor(db_engine, "windows-only")
    offering = await add_session_type(db_engine, mentor, duration=60)
    await seed_windows(db_engine, offering, "17:00", "19:00")

    assert len(await starts(api_client, mentor, offering)) == 2


async def test_exceptions_still_subtract_when_windows_are_in_effect(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Windows replace availability, not unavailability.**

    A mentor who blocked a date blocked it for every offering, and the read that
    switched sources for `rules` deliberately did not switch it for `exceptions`.
    That is derived rather than stated in the decision, which is exactly why it
    needs a test: nothing in the phrase "windows replace general availability"
    says so, and a reader implementing it from the sentence alone would switch
    both.
    """
    mentor = await make_public_mentor(db_engine, "windows-blocked")
    offering = await add_session_type(db_engine, mentor, duration=60)
    await seed_windows(db_engine, offering, "17:00", "19:00")
    await add_block(db_engine, mentor, DAY)

    assert await starts(api_client, mentor, offering) == []


async def test_a_switched_off_window_falls_back_to_general_availability(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The index and the exclusion constraint are both partial on
    `is_active AND deleted_at IS NULL`, and so is the query.

    An offering whose only window is switched off has no *live* windows, so it
    has no windows — and falls back, rather than becoming unbookable.
    """
    mentor = await make_public_mentor(db_engine, "windows-off")
    offering = await add_session_type(db_engine, mentor, duration=60)
    await seed_availability(db_engine, mentor, "09:00", "11:00")
    await seed_windows(db_engine, offering, "17:00", "19:00", active=False)

    assert len(await starts(api_client, mentor, offering)) == 2


async def test_two_offerings_keep_their_own_windows(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The whole point of per-offering scheduling, and the thing a query scoped
    on the mentor rather than the offering would break — while every test above
    still passed, because each uses one windowed offering."""
    mentor = await make_public_mentor(db_engine, "windows-two")
    morning = await add_session_type(db_engine, mentor, name="Morning", duration=60)
    evening = await add_session_type(db_engine, mentor, name="Evening", duration=60)
    await seed_windows(db_engine, morning, "09:00", "12:00")
    await seed_windows(db_engine, evening, "17:00", "19:00")

    assert len(await starts(api_client, mentor, morning)) == 3
    assert len(await starts(api_client, mentor, evening)) == 2
