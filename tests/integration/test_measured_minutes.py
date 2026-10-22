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
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tests.integration.test_api_attendance import settle
from tests.integration.test_api_meeting_provisioning import a_mentor_on, started
from tests.integration.test_api_session_door import attendance_of, status_of
from tests.integration.test_presence_attendance import room_and_users, seen
from tests.off_the_loop import guarded
from tests.unit.test_daily_rooms import rooms as daily_rooms

from app.core.config import Settings
from app.domain.session_minutes import co_present_minutes
from app.infra.clients.daily_presence import Sighting, Stay
from app.infra.clients.meetings import DailyRooms, VenueUnavailableError
from app.infra.db.session_stats import mentee_window_figures, mentor_stats
from app.infra.db.session_writer import measure_minutes
from app.infra.jobs import runner
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


class FailingFor(StayRecords):
    """Records that break unexpectedly for one room only."""

    def __init__(self, stays: dict[str, list[Stay]], *, broken: str):
        super().__init__(stays)
        self.broken = broken

    def stays(self, room: str) -> list[Stay]:
        if room == self.broken:
            self.asked.append(room)
            raise RuntimeError("an unexpected failure for one session")
        return super().stays(room)


async def held(
    engine: AsyncEngine,
    client: httpx.AsyncClient,
    tag: str,
    *,
    ended_minutes_ago: int = 30,
    settled: bool = True,
) -> dict[str, Any]:
    """A 60-minute Daily session both parties were seen in, whose end was
    `ended_minutes_ago` minutes ago: settled completed, or with `settled` off
    still waiting for the settlement job."""
    setup = await a_mentor_on(engine, tag, "daily")
    session = await started(engine, client, setup, minutes_ago=60 + ended_minutes_ago)
    venue = await room_and_users(engine, session["id"])
    for user in (venue["mentee_id"], venue["mentor_id"]):
        await seen(engine, Sighting(venue["room"], str(user), venue["starts_at"]))
    if settled:
        await settle(engine)
    return {"id": session["id"], **venue}


async def own_room(engine: AsyncEngine, venue: dict[str, Any], room: str) -> None:
    """Move `venue` to a room of its own: the fake door names every room alike,
    and records are answered per room."""
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET external_room_id = :r WHERE id = :i"),
            {"r": room, "i": venue["id"]},
        )
    venue["room"] = room


def daily_answering(records: dict[str, dict[str, Any]]) -> DailyRooms:
    """The real Daily adapter with `/meetings` answered per room from
    `records`, so the parse and its refusals run as in production."""

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=records[request.url.params["room"]])

    adapter, _ = daily_rooms(answer)
    return adapter


def finished_meeting(venue: dict[str, Any], seconds: float) -> dict[str, Any]:
    """Daily's records of one finished meeting both parties stayed `seconds` in."""
    joined = venue["starts_at"].timestamp()
    return {
        "data": [
            {
                "ongoing": False,
                "participants": [
                    {"user_id": str(user), "join_time": joined, "duration": seconds}
                    for user in (venue["mentor_id"], venue["mentee_id"])
                ],
            }
        ]
    }


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
    engine: AsyncEngine, records: StayRecords | DailyRooms, *, budget: dt.timedelta | None = None
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
async def test_an_unconfigured_provider_stops_asking_and_is_not_fatal(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """No provider means no session can be read this run, so it asks once and
    stops rather than logging every due session."""
    first = await held(db_engine, api_client, "mm-noprov-1")
    second = await held(db_engine, api_client, "mm-noprov-2")
    records = StayRecords(error=NotImplementedError("no provider"))

    await measure(db_engine, records)

    assert len(records.asked) == 1
    assert await measured_of(db_engine, first["id"]) is None
    assert await measured_of(db_engine, second["id"]) is None


@pytest.mark.usefixtures("door")
async def test_a_party_is_matched_as_a_uuid_not_as_text(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The same parse attendance uses: an id echoed in another spelling of the
    same UUID is still that party, so the measure is not silently zero."""
    venue = await held(db_engine, api_client, "mm-uuid")
    shouted = [
        Stay(s.user_id.upper(), s.joined, s.left)
        for s in stays(venue, mentor=(0, 50), mentee=(0, 50))
    ]

    await measure(db_engine, StayRecords({venue["room"]: shouted}))

    assert await measured_of(db_engine, venue["id"]) == 50


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


async def measuring_breaks(session: AsyncSession, **__: Any) -> int:
    """The minutes phase failing in Postgres, which aborts the transaction:
    what the job's error boundary has to recover from, not a bare raise."""
    await session.execute(text("SELECT 1 / 0"))
    raise AssertionError("division by zero should have failed in Postgres")


async def pending_writes(session: AsyncSession, session_id: str) -> tuple[str, int]:
    """The session's status and its queued review requests as `session` sees
    them, uncommitted writes included."""
    status = (
        await session.execute(text("SELECT status FROM sessions WHERE id = :i"), {"i": session_id})
    ).scalar_one()
    queued = (
        await session.execute(
            text(
                "SELECT count(*) FROM outbox_events "
                "WHERE entity_id = :i AND event_type = 'review_requested'"
            ),
            {"i": session_id},
        )
    ).scalar_one()
    return str(status), int(queued)


@pytest.mark.usefixtures("door")
async def test_a_failure_measuring_minutes_does_not_stop_the_settlement_run(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**Its own error boundary.** Minutes are a stats refinement: the drain
    that sends this run's messages comes after them and must still run, and the
    attendance this run settled before them stays settled."""
    venue = await held(db_engine, api_client, "mm-boundary", settled=False)
    drained: list[bool] = []

    async def drain_records(*_: Any, **__: Any) -> dict[str, int]:
        drained.append(True)
        return {}

    monkeypatch.setattr(runner, "measure_minutes", measuring_breaks)
    monkeypatch.setattr(runner, "drain", drain_records)
    monkeypatch.setattr(RuntimeJobs, "_rooms", lambda _self: StayRecords())

    jobs = RuntimeJobs(Settings(_env_file=None, database_url=SecretStr(migrated_database)))
    await jobs.run("settle-sessions")

    assert drained == [True]
    assert await status_of(db_engine, venue["id"]) == "completed"
    assert [row["attendance_status"] for row in await attendance_of(db_engine, venue["id"])] == [
        "attended",
        "attended",
    ]


@pytest.mark.usefixtures("door")
async def test_a_dry_run_keeps_what_it_settled_when_measuring_fails(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**A dry run commits nothing before the minutes phase**, so a failure there
    rolls back to a savepoint, never the whole session: the drain still counts
    the settlement and its review request, and the run still changes nothing."""
    venue = await held(db_engine, api_client, "mm-dry", settled=False)
    seen_by_drain: list[tuple[str, int]] = []

    async def drain_counts(session: AsyncSession, **__: Any) -> dict[str, int]:
        seen_by_drain.append(await pending_writes(session, venue["id"]))
        return {}

    monkeypatch.setattr(runner, "measure_minutes", measuring_breaks)
    monkeypatch.setattr(runner, "drain", drain_counts)
    monkeypatch.setattr(RuntimeJobs, "_rooms", lambda _self: StayRecords())

    jobs = RuntimeJobs(Settings(_env_file=None, database_url=SecretStr(migrated_database)))
    await jobs.run("settle-sessions", dry_run=True)

    assert seen_by_drain == [("completed", 1)]
    assert await status_of(db_engine, venue["id"]) == "confirmed"


@pytest.mark.usefixtures("door")
async def test_a_poison_record_is_skipped_and_the_rest_are_measured(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**A finite but impossible duration** is unreadable, not a stored figure:
    `2e11` seconds fits a datetime and overflows the stored minutes. Read first,
    as the oldest, it must not hold back the session after it."""
    poisoned = await held(db_engine, api_client, "mm-poison", ended_minutes_ago=90)
    normal = await held(db_engine, api_client, "mm-poison-ok")
    await own_room(db_engine, poisoned, "room-poisoned")
    rooms = daily_answering(
        {
            poisoned["room"]: finished_meeting(poisoned, 2e11),
            normal["room"]: finished_meeting(normal, 70 * 60),
        }
    )

    assert await measure(db_engine, rooms) == 1

    assert await measured_of(db_engine, normal["id"]) == 70
    assert await measured_of(db_engine, poisoned["id"]) is None


@pytest.mark.usefixtures("door")
async def test_a_write_the_database_refuses_is_skipped_not_fatal(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**Each write is its own savepoint.** A write Postgres refuses aborts the
    transaction; without one every later write in the run would fail with it.
    Forced here through the column's own CHECK on the oldest session."""
    refused = await held(db_engine, api_client, "mm-refused", ended_minutes_ago=90)
    normal = await held(db_engine, api_client, "mm-refused-ok")
    await own_room(db_engine, refused, "room-refused")
    records = StayRecords(
        {
            refused["room"]: stays(refused, mentor=(0, 60), mentee=(0, 60)),
            normal["room"]: stays(normal, mentor=(0, 66), mentee=(0, 66)),
        }
    )
    calls: list[int] = []

    def refused_first(*args: Any) -> int:
        calls.append(1)
        return -1 if len(calls) == 1 else co_present_minutes(*args)

    monkeypatch.setattr("app.infra.db.session_writer.minutes.co_present_minutes", refused_first)

    assert await measure(db_engine, records) == 1

    assert await measured_of(db_engine, normal["id"]) == 66
    assert await measured_of(db_engine, refused["id"]) is None


@pytest.mark.usefixtures("door")
async def test_one_session_failing_unexpectedly_does_not_stop_the_others(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Every per-session failure is a per-session skip**: the oldest session
    breaking in a way nobody foresaw is logged and passed over, and the next
    run asks it again."""
    broken = await held(db_engine, api_client, "mm-broken", ended_minutes_ago=90)
    normal = await held(db_engine, api_client, "mm-broken-ok")
    await own_room(db_engine, broken, "room-broken")
    records = FailingFor(
        {normal["room"]: stays(normal, mentor=(0, 65), mentee=(0, 65))}, broken=broken["room"]
    )

    assert await measure(db_engine, records) == 1

    assert records.asked == [broken["room"], normal["room"]]
    assert await measured_of(db_engine, normal["id"]) == 65
    assert await measured_of(db_engine, broken["id"]) is None
