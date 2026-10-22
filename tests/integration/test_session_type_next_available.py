"""Each session type's own next free time, on the public profile (decision 284).

The mentor-level `next_available_*` pair, per offering: stored by the same
refresh in the same transaction, vouched for by the same change log, and the
mentor's time is always the earliest of its offerings'. Every test here is a
way the per-offering value could disagree with the mentor's, or outlive what
the platform knows.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import add_availability, add_session, add_session_type
from tests.integration.test_mentor_next_available import FakeCalendar, refresh
from tests.integration.test_next_available_session_type import two_offerings

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def profile(client: httpx.AsyncClient, mentor: UUID) -> dict[str, Any]:
    response = await client.get(f"/api/v1/mentors/{mentor}")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def by_id(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["id"]: row for row in body["session_types"]}


def instant(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


async def stored(engine: AsyncEngine, mentor: UUID) -> dict[UUID, tuple[Any, ...]]:
    """Every per-offering row of a mentor, by offering: (id, time, deadline)."""
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT session_type_id, id, next_available_at, bookable_until "
                    "FROM session_type_next_availability WHERE mentor_user_id = :m"
                ),
                {"m": mentor},
            )
        ).all()
    return {row[0]: tuple(row[1:]) for row in rows}


async def test_each_offering_carries_its_own_time_and_the_mentor_the_earliest(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, slow, quick = await two_offerings(db_engine, "per-type-open")

    await refresh(db_engine)
    body = await profile(api_client, mentor)
    types = by_id(body)

    assert types[str(slow)]["next_available_state"] == "open"
    assert types[str(quick)]["next_available_state"] == "open"
    # Three days' notice puts the slow offering's first slot after the quick one's.
    assert instant(types[str(slow)]["next_available_at"]) > instant(
        types[str(quick)]["next_available_at"]
    )
    assert instant(body["next_available_at"]) == instant(types[str(quick)]["next_available_at"])
    # The standalone list is the same statement, so it carries the same values.
    listed = (await api_client.get(f"/api/v1/users/{mentor}/session-types")).json()["data"]
    assert {row["id"]: row["next_available_at"] for row in listed} == {
        key: row["next_available_at"] for key, row in types.items()
    }


async def test_the_mentors_row_is_the_earliest_of_its_offerings_rows(
    db_engine: AsyncEngine,
) -> None:
    """The invariant, on the stored rows themselves: time, deadline and
    offering all come from the earliest offering's row."""
    mentor, _slow, quick = await two_offerings(db_engine, "per-type-min")

    await refresh(db_engine)
    async with db_engine.begin() as conn:
        parent = (
            await conn.execute(
                text(
                    "SELECT next_available_at, bookable_until, next_available_session_type_id "
                    "FROM mentor_next_availability WHERE mentor_user_id = :m"
                ),
                {"m": mentor},
            )
        ).one()
        earliest = (
            await conn.execute(
                text(
                    "SELECT next_available_at, bookable_until, session_type_id "
                    "FROM session_type_next_availability WHERE mentor_user_id = :m "
                    "ORDER BY next_available_at LIMIT 1"
                ),
                {"m": mentor},
            )
        ).one()

    assert tuple(parent) == tuple(earliest)
    assert parent[2] == quick
    assert len(await stored(db_engine, mentor)) == 2


async def test_a_change_after_the_refresh_makes_every_offering_refreshing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, _slow, _quick = await two_offerings(db_engine, "per-type-booked")
    await refresh(db_engine)
    await add_session(db_engine, mentor, status="confirmed", days_ago=-3)

    types = by_id(await profile(api_client, mentor))

    assert {row["next_available_state"] for row in types.values()} == {"refreshing"}
    assert {row["next_available_at"] for row in types.values()} == {None}


async def test_one_offerings_passed_deadline_leaves_the_other_open(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, slow, quick = await two_offerings(db_engine, "per-type-deadline")
    await refresh(db_engine)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE session_type_next_availability "
                "SET bookable_until = now() - interval '1 minute' WHERE session_type_id = :t"
            ),
            {"t": quick},
        )

    types = by_id(await profile(api_client, mentor))

    assert types[str(quick)]["next_available_state"] == "refreshing"
    assert types[str(quick)]["next_available_at"] is None
    assert types[str(slow)]["next_available_state"] == "open"
    assert types[str(slow)]["next_available_at"] is not None


async def test_an_offering_with_nothing_free_is_none_beside_an_open_one(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Four hours never fit the mentor's three-hour weekly windows."""
    mentor, slow, quick = await two_offerings(db_engine, "per-type-none")
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE session_type_booking_configs "
                "SET duration_minutes = 240 "
                "WHERE session_type_id = :t"
            ),
            {"t": slow},
        )

    await refresh(db_engine)
    body = await profile(api_client, mentor)
    types = by_id(body)

    assert types[str(slow)]["next_available_state"] == "none"
    assert types[str(slow)]["next_available_at"] is None
    assert types[str(quick)]["next_available_state"] == "open"
    assert body["next_available_state"] == "open"


async def test_an_offering_added_since_the_refresh_reads_refreshing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Adding an offering logs a change, so its mentor reads `refreshing` too;
    the log is cleared here to show the missing row alone is enough — the
    state of every offering before the first refresh after deploy."""
    mentor, _slow, quick = await two_offerings(db_engine, "per-type-added")
    await refresh(db_engine)
    added = await add_session_type(db_engine, mentor, name="M added later", notice=0)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM mentor_availability_changes WHERE mentor_user_id = :m"),
            {"m": mentor},
        )

    types = by_id(await profile(api_client, mentor))

    assert types[str(added)]["next_available_state"] == "refreshing"
    assert types[str(added)]["next_available_at"] is None
    assert types[str(quick)]["next_available_state"] == "open"


async def test_a_mentor_not_taking_bookings_reads_none_on_every_offering(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """No weekly hours: listed, offering things, bookable by nobody."""
    mentor, _slow, _quick = await two_offerings(db_engine, "per-type-closed")
    await refresh(db_engine)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM availability_rules WHERE mentor_user_id = :m"), {"m": mentor}
        )

    types = by_id(await profile(api_client, mentor))

    assert {row["next_available_state"] for row in types.values()} == {"none"}


async def test_a_retired_offerings_row_goes_at_the_next_refresh(db_engine: AsyncEngine) -> None:
    mentor, slow, quick = await two_offerings(db_engine, "per-type-retired")
    await refresh(db_engine)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE session_types SET is_active = false WHERE id = :t"), {"t": slow}
        )

    await refresh(db_engine)

    assert set(await stored(db_engine, mentor)) == {quick}


async def test_an_older_run_leaves_the_offerings_rows_alone(db_engine: AsyncEngine) -> None:
    """The overlap guard covers the offerings' rows: a refused write replaces
    none of them, so a mentor's rows never come from two runs."""
    mentor, _slow, _quick = await two_offerings(db_engine, "per-type-overlap")
    newer = dt.datetime.now(dt.UTC)
    await refresh(db_engine, now=newer)
    before = await stored(db_engine, mentor)
    assert len(before) == 2
    await add_session(db_engine, mentor, status="confirmed", days_ago=-3)

    counts = await refresh(db_engine, now=newer - dt.timedelta(minutes=2))

    assert counts["superseded"] == 1
    assert await stored(db_engine, mentor) == before


async def test_a_failed_calendar_read_leaves_the_offerings_rows_alone(
    db_engine: AsyncEngine,
) -> None:
    mentor, _slow, _quick = await two_offerings(db_engine, "per-type-failed")
    await refresh(db_engine)
    before = await stored(db_engine, mentor)
    assert len(before) == 2
    await add_availability(db_engine, mentor, day_of_week=3, start="14:00", end="16:00")

    counts = await refresh(db_engine, calendar=FakeCalendar(fails_for=mentor))

    assert counts["failed"] == 1
    assert await stored(db_engine, mentor) == before


async def test_the_profile_reads_them_without_a_statement_per_offering(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The per-offering state rides in the statement that lists the offerings,
    so a mentor with three costs what a mentor with two does."""
    two, _slow, _quick = await two_offerings(db_engine, "per-type-n1-two")
    three, _slow, _quick = await two_offerings(db_engine, "per-type-n1-three")
    await add_session_type(db_engine, three, name="M third", notice=0)
    await refresh(db_engine)
    statements: list[str] = []

    def record(*args: Any) -> None:
        statements.append(args[2])

    counts = []
    for mentor in (two, three):
        statements.clear()
        event.listen(db_engine.sync_engine, "before_cursor_execute", record)
        try:
            body = await profile(api_client, mentor)
        finally:
            event.remove(db_engine.sync_engine, "before_cursor_execute", record)
        assert {row["next_available_state"] for row in body["session_types"]} == {"open"}
        counts.append(len(statements))

    assert counts[0] == counts[1], statements


async def test_the_next_refresh_moves_an_offerings_time_in_place(db_engine: AsyncEngine) -> None:
    """A longer notice pushes the quick offering's first slot later; the row is
    updated where it stands rather than replaced."""
    mentor, _slow, quick = await two_offerings(db_engine, "per-type-moved")
    await refresh(db_engine)
    before = (await stored(db_engine, mentor))[quick]
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE session_type_booking_configs SET min_notice_minutes = 4320 "
                "WHERE session_type_id = :t"
            ),
            {"t": quick},
        )

    await refresh(db_engine)
    after = (await stored(db_engine, mentor))[quick]

    assert after[0] == before[0]
    assert after[1] > before[1]
