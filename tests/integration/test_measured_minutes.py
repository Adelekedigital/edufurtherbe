"""Measured minutes for EduFurther video sessions (Integrations PRD, decision 4).

Once a completed Daily session's call is over, the settlement job reads how
long the mentor and the mentee were in the room together and stores it; the
stats then count the longer of that and the booked length. Meet and migrated
sessions are never read and count their booked length.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.test_api_attendance import settle
from tests.integration.test_api_meeting_provisioning import a_mentor_on, started
from tests.integration.test_presence_attendance import room_and_users, seen
from tests.off_the_loop import guarded

from app.core.config import Settings
from app.infra.clients.daily_presence import Sighting, Stay
from app.infra.clients.meetings import VenueUnavailableError
from app.infra.db.session_stats import mentee_window_figures, mentor_stats
from app.infra.db.session_writer import measure_minutes
from app.infra.jobs.runner import RuntimeJobs

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


class StayRecords:
    """Daily's records of finished meetings, faked: each room's stays, or a failure."""

    def __init__(
        self, stays: dict[str, list[Stay]] | None = None, *, error: Exception | None = None
    ):
        self.stays_by_room = stays or {}
        self.error = error
        self.asked: list[str] = []

    def stays(self, room: str) -> list[Stay]:
        self.asked.append(room)
        if self.error is not None:
            raise self.error
        return self.stays_by_room.get(room, [])

    def sightings(self, room: str) -> list[Sighting]:
        """The presence check's read, for a job run: these sessions are already
        settled, so it is never asked."""
        raise AssertionError(f"no presence read expected for {room}")


async def held(
    engine: AsyncEngine, client: httpx.AsyncClient, tag: str, *, ended_minutes_ago: int = 30
) -> dict[str, Any]:
    """A 60-minute Daily session both parties were seen in, settled completed,
    whose end was `ended_minutes_ago` minutes ago."""
    setup = await a_mentor_on(engine, tag, "daily")
    session = await started(engine, client, setup, minutes_ago=60 + ended_minutes_ago)
    venue = await room_and_users(engine, session["id"])
    for user in (venue["mentee_id"], venue["mentor_id"]):
        await seen(engine, Sighting(venue["room"], str(user), venue["starts_at"]))
    await settle(engine)
    return {"id": session["id"], **venue}


def stays(venue: dict[str, Any], mentor: tuple[int, int], mentee: tuple[int, int]) -> list[Stay]:
    """Each party's stay as minutes from the session's start."""
    start = venue["starts_at"]

    def one(user: Any, span: tuple[int, int]) -> Stay:
        return Stay(
            str(user),
            start + dt.timedelta(minutes=span[0]),
            start + dt.timedelta(minutes=span[1]),
        )

    return [one(venue["mentor_id"], mentor), one(venue["mentee_id"], mentee)]


async def measure(
    engine: AsyncEngine, records: StayRecords, *, budget: dt.timedelta | None = None
) -> int:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        kwargs = {} if budget is None else {"budget": budget}
        measured = await measure_minutes(
            session,
            now=dt.datetime.now(dt.UTC),
            rooms=guarded("meeting_rooms", records),
            **kwargs,
        )
        await session.commit()
    return measured


async def measured_of(engine: AsyncEngine, session_id: str) -> int | None:
    async with engine.connect() as conn:
        value: int | None = (
            await conn.execute(
                text("SELECT measured_minutes FROM sessions WHERE id = :i"), {"i": session_id}
            )
        ).scalar_one()
    return value


async def minutes_both_sides(engine: AsyncEngine, venue: dict[str, Any]) -> tuple[int, int]:
    """`mentoring_minutes` on the mentor's stats and the mentee's minutes of
    guidance: one definition, so they must agree."""
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        mentor = await mentor_stats(session, venue["mentor_id"])
        _, minutes, _ = mentee_window_figures(venue["mentee_id"], None)
        mentee = (await session.execute(select(minutes))).scalar_one()
    return int(mentor["mentoring_minutes"]), int(mentee)


@pytest.mark.usefixtures("door")
async def test_an_overrun_counts_its_measured_minutes(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    venue = await held(db_engine, api_client, "mm-overrun")
    records = StayRecords({venue["room"]: stays(venue, mentor=(-2, 75), mentee=(0, 70))})

    assert await measure(db_engine, records) == 1

    assert records.asked == [venue["room"]]
    assert await measured_of(db_engine, venue["id"]) == 70
    assert await minutes_both_sides(db_engine, venue) == (70, 70)


@pytest.mark.usefixtures("door")
async def test_a_short_call_still_counts_its_booked_length(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    venue = await held(db_engine, api_client, "mm-short")
    records = StayRecords({venue["room"]: stays(venue, mentor=(0, 25), mentee=(0, 25))})

    await measure(db_engine, records)

    assert await measured_of(db_engine, venue["id"]) == 25
    assert await minutes_both_sides(db_engine, venue) == (60, 60)


@pytest.mark.usefixtures("door")
async def test_one_party_waiting_alone_measures_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    venue = await held(db_engine, api_client, "mm-alone")
    records = StayRecords({venue["room"]: stays(venue, mentor=(0, 30), mentee=(40, 60))})

    await measure(db_engine, records)

    assert await measured_of(db_engine, venue["id"]) == 0
    assert await minutes_both_sides(db_engine, venue) == (60, 60)


@pytest.mark.usefixtures("door")
async def test_a_stranger_in_the_records_is_not_a_party(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Only the session's own mentor and mentee count: a stay under any other
    id is somebody else, however long they stayed."""
    venue = await held(db_engine, api_client, "mm-stranger")
    start = venue["starts_at"]
    stranger = Stay("not-a-party", start, start + dt.timedelta(minutes=90))
    records = StayRecords(
        {venue["room"]: [*stays(venue, mentor=(0, 90), mentee=(0, 10)), stranger]}
    )

    await measure(db_engine, records)

    assert await measured_of(db_engine, venue["id"]) == 10


@pytest.mark.usefixtures("door")
async def test_unreadable_records_count_booked_and_are_asked_again(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A meeting still going, or a body that cannot be read: nothing is stored,
    so the booked length counts, and the next run asks again."""
    venue = await held(db_engine, api_client, "mm-unread")
    records = StayRecords(error=VenueUnavailableError("a meeting is still going"))

    assert await measure(db_engine, records) == 0
    await measure(db_engine, records)

    assert records.asked == [venue["room"], venue["room"]]
    assert await measured_of(db_engine, venue["id"]) is None
    assert await minutes_both_sides(db_engine, venue) == (60, 60)


@pytest.mark.usefixtures("door")
async def test_an_unconfigured_provider_is_unreadable_not_fatal(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    venue = await held(db_engine, api_client, "mm-noprovider")

    await measure(db_engine, StayRecords(error=NotImplementedError("no provider")))

    assert await measured_of(db_engine, venue["id"]) is None


@pytest.mark.usefixtures("door")
async def test_a_measured_session_is_not_read_again(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    venue = await held(db_engine, api_client, "mm-once")
    first = StayRecords({venue["room"]: stays(venue, mentor=(0, 70), mentee=(0, 70))})
    await measure(db_engine, first)
    again = StayRecords({venue["room"]: stays(venue, mentor=(0, 5), mentee=(0, 5))})

    await measure(db_engine, again)

    assert again.asked == []
    assert await measured_of(db_engine, venue["id"]) == 70


@pytest.mark.usefixtures("door")
async def test_records_are_not_read_until_the_session_is_over_and_written(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    venue = await held(db_engine, api_client, "mm-early", ended_minutes_ago=1)
    records = StayRecords({venue["room"]: stays(venue, mentor=(0, 60), mentee=(0, 60))})

    await measure(db_engine, records)

    assert records.asked == []
    assert await measured_of(db_engine, venue["id"]) is None


@pytest.mark.usefixtures("door")
async def test_after_a_day_the_session_keeps_its_booked_length(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    venue = await held(db_engine, api_client, "mm-late", ended_minutes_ago=24 * 60)
    records = StayRecords({venue["room"]: stays(venue, mentor=(0, 90), mentee=(0, 90))})

    await measure(db_engine, records)

    assert records.asked == []
    assert await measured_of(db_engine, venue["id"]) is None


#: What takes a session out of measuring, as whole statements.
CHANGES = {
    "meet": "UPDATE sessions SET meeting_provider = 'google_meet' WHERE id = :i",
    "migrated, no room": "UPDATE sessions SET external_room_id = NULL WHERE id = :i",
    "not completed": "UPDATE sessions SET status = 'no_show' WHERE id = :i",
}


@pytest.mark.usefixtures("door")
@pytest.mark.parametrize("change", sorted(CHANGES))
async def test_only_completed_daily_sessions_with_a_room_are_read(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, change: str
) -> None:
    """Meet reports no stays and a migrated session has no room: no Daily
    call is made for either, and both count their booked length."""
    venue = await held(db_engine, api_client, f"mm-skip-{change[:4].strip()}")
    async with db_engine.begin() as conn:
        await conn.execute(text(CHANGES[change]), {"i": venue["id"]})
    records = StayRecords({venue["room"]: stays(venue, mentor=(0, 90), mentee=(0, 90))})

    await measure(db_engine, records)

    assert records.asked == []
    assert await measured_of(db_engine, venue["id"]) is None


@pytest.mark.usefixtures("door")
async def test_once_the_read_budget_is_spent_the_rest_wait(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    venue = await held(db_engine, api_client, "mm-budget")
    records = StayRecords({venue["room"]: stays(venue, mentor=(0, 70), mentee=(0, 70))})

    await measure(db_engine, records, budget=dt.timedelta())

    assert records.asked == []
    assert await measured_of(db_engine, venue["id"]) is None


@pytest.mark.usefixtures("door")
async def test_the_settlement_job_measures_a_finished_session(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wired into `settle-sessions`, after attendance is committed."""
    venue = await held(db_engine, api_client, "mm-job")
    records = StayRecords({venue["room"]: stays(venue, mentor=(0, 72), mentee=(0, 72))})
    monkeypatch.setattr(RuntimeJobs, "_rooms", lambda _self: records)

    jobs = RuntimeJobs(Settings(_env_file=None, database_url=SecretStr(migrated_database)))
    await jobs.run("settle-sessions")

    assert await measured_of(db_engine, venue["id"]) == 72
