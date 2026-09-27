"""A mentor's next free time on the discovery card, stored and refreshed.

**What the card promises is narrow**, and every test here is about keeping it:
`next_available_at` is either a time the booking flow would also offer, or
`null`. Never a time that has since been taken. So the tests that matter most
are the ones where something changes *after* a refresh — a booking, an hours
change, a change that lands while the refresh is running — and the card must go
to `refreshing` rather than keep showing what it knew.

The refresh is called directly with a fake calendar reader. The QStash wiring is
pinned by the runtime-job unit tests; what is tested here is what it runs.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import Column, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.sql import visitors
from tests.integration.factories import (
    add_availability,
    add_session,
    add_session_type,
    make_bookable_mentor,
    make_public_mentor,
)
from tests.integration.test_api_freebusy import KEY, connect_calendar

from app.domain.availability import UtcInterval
from app.infra.clients.meetings import VenueUnavailableError
from app.infra.db.calendar_store import MentorFreeBusy
from app.infra.db.next_available_store import JITTER, refresh_next_available
from app.infra.db.public_visibility import mentor_is_public

pytestmark = [pytest.mark.db, pytest.mark.anyio]

URL = "/api/v1/mentors"
MAX_AGE = dt.timedelta(minutes=5)

#: Every table whose rows change when a mentor is free, and so must log a
#: change. Pinned against `pg_trigger` rather than exercised one insert at a time:
#: the failure this guards is a table *missing* from the list, which a test of
#: the tables somebody remembered cannot see.
TRIGGERED = {
    "sessions",
    "availability_rules",
    "availability_exceptions",
    "session_types",
    "session_type_booking_configs",
    "session_type_scheduling_windows",
    "mentor_profiles",
    "users",
    "calendar_connections",
}


class FakeCalendar:
    """A `FreeBusyReader` that reports fixed busy intervals and counts its calls.

    `during` runs inside the call — the only point in a refresh where the test
    can make a change land *after* the refresh read the row and *before* it
    wrote its answer.
    """

    def __init__(
        self,
        busy: tuple[UtcInterval, ...] = (),
        during: Callable[[], Awaitable[None]] | None = None,
        fails_for: UUID | None = None,
    ) -> None:
        self.busy_intervals = busy
        self.during = during
        self.fails_for = fails_for
        self.calls = 0

    async def busy(
        self, session: AsyncSession, user_id: UUID, start: dt.datetime, end: dt.datetime
    ) -> tuple[UtcInterval, ...]:
        del session, start, end
        self.calls += 1
        if user_id == self.fails_for:
            raise RuntimeError("calendar unreachable")
        if self.during is not None:
            await self.during()
        return self.busy_intervals


async def refresh(
    engine: AsyncEngine,
    *,
    now: dt.datetime | None = None,
    calendar: Any = None,
    dry_run: bool = False,
) -> dict[str, int]:
    async with AsyncSession(engine) as session:
        return await refresh_next_available(
            session,
            now=now or dt.datetime.now(dt.UTC),
            max_age=MAX_AGE,
            reader=calendar or FakeCalendar(),
            dry_run=dry_run,
        )


async def card(client: httpx.AsyncClient, mentor: UUID) -> dict[str, Any]:
    body = (await client.get(URL)).json()
    return next(row for row in body["data"] if row["id"] == str(mentor))


async def first_slot(client: httpx.AsyncClient, mentor: UUID) -> str:
    """What the booking flow offers first, from the public slots endpoint."""
    types = (await client.get(f"/api/v1/users/{mentor}/session-types")).json()["data"]
    response = await client.get(
        f"/api/v1/users/{mentor}/availability/slots",
        params={"session_type_id": types[0]["id"]},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"][0]["start"]


def same_instant(a: str, b: str) -> bool:
    return dt.datetime.fromisoformat(a) == dt.datetime.fromisoformat(b)


# --------------------------------------------------------------------------
# The three states
# --------------------------------------------------------------------------


async def test_a_mentor_never_computed_is_refreshing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "next-new")

    row = await card(api_client, mentor)

    assert row["next_available_state"] == "refreshing"
    assert row["next_available_at"] is None


async def test_after_a_refresh_the_card_shows_what_booking_offers_first(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The same computation as `/slots`, so the card cannot promise a time the
    booking flow would not offer."""
    mentor = await make_bookable_mentor(db_engine, "next-open")

    await refresh(db_engine)
    row = await card(api_client, mentor)

    assert row["next_available_state"] == "open"
    assert same_instant(row["next_available_at"], await first_slot(api_client, mentor))


async def test_nothing_free_in_the_horizon_is_none(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "next-none")
    now = dt.datetime.now(dt.UTC)
    fully_booked = FakeCalendar(
        (UtcInterval(start=now - dt.timedelta(days=2), end=now + dt.timedelta(days=90)),)
    )

    await refresh(db_engine, now=now, calendar=fully_booked)
    row = await card(api_client, mentor)

    assert row["next_available_state"] == "none"
    assert row["next_available_at"] is None


async def test_the_calendar_is_read_once_per_mentor_not_once_per_offering(
    db_engine: AsyncEngine,
) -> None:
    mentor = await make_bookable_mentor(db_engine, "next-two-offerings")
    await add_session_type(db_engine, mentor, name="Essay review")
    calendar = FakeCalendar()

    await refresh(db_engine, calendar=calendar)

    assert calendar.calls == 1


# --------------------------------------------------------------------------
# Going stale
# --------------------------------------------------------------------------


async def test_a_booking_after_a_refresh_makes_the_card_refreshing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "next-booked")
    await refresh(db_engine)
    assert (await card(api_client, mentor))["next_available_state"] == "open"

    await add_session(db_engine, mentor, status="confirmed", days_ago=-3)
    row = await card(api_client, mentor)

    assert row["next_available_state"] == "refreshing"
    assert row["next_available_at"] is None


async def test_an_hours_change_after_a_refresh_makes_the_card_refreshing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "next-hours")
    await refresh(db_engine)

    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE availability_rules SET end_time = '11:00' WHERE mentor_user_id = :m"),
            {"m": mentor},
        )

    assert (await card(api_client, mentor))["next_available_state"] == "refreshing"


async def test_the_next_refresh_brings_it_back(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "next-back")
    await refresh(db_engine)
    await add_session(db_engine, mentor, status="confirmed", days_ago=-3)

    await refresh(db_engine)

    assert (await card(api_client, mentor))["next_available_state"] == "open"


async def test_a_booking_committed_during_a_refresh_leaves_it_stale(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The refresh read the row before the booking committed, and computed from
    data without it. Its answer must not be vouched for — which is what the
    first version, comparing clocks, got wrong."""
    mentor = await make_bookable_mentor(db_engine, "next-race")

    async def book() -> None:
        await add_session(db_engine, mentor, status="confirmed", days_ago=-3)

    await refresh(db_engine, calendar=FakeCalendar(during=book))

    assert (await card(api_client, mentor))["next_available_state"] == "refreshing"


async def test_a_slot_past_its_booking_deadline_is_not_shown(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "next-passed")
    await refresh(db_engine)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE mentor_next_availability "
                "SET bookable_until = now() - interval '1 minute' WHERE mentor_user_id = :m"
            ),
            {"m": mentor},
        )

    row = await card(api_client, mentor)

    assert row["next_available_state"] == "refreshing"
    assert row["next_available_at"] is None


async def test_every_availability_table_logs_a_change(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        tables = set(
            (
                await conn.execute(
                    text(
                        "SELECT c.relname FROM pg_trigger t "
                        "JOIN pg_class c ON c.oid = t.tgrelid "
                        "WHERE t.tgname = 'trg_log_availability_change' AND NOT t.tgisinternal"
                    )
                )
            ).scalars()
        )

    assert tables == TRIGGERED


# --------------------------------------------------------------------------
# What the job refreshes
# --------------------------------------------------------------------------


async def computed_at(engine: AsyncEngine, mentor: UUID) -> dt.datetime:
    async with engine.begin() as conn:
        value = await conn.execute(
            text("SELECT computed_at FROM mentor_next_availability WHERE mentor_user_id = :m"),
            {"m": mentor},
        )
        return value.scalar_one()


async def test_a_fresh_value_is_left_until_it_reaches_the_maximum_age(
    db_engine: AsyncEngine,
) -> None:
    """Google-side changes fire no trigger, so age is the only thing that
    catches them — and a refresh that recomputed everything every run would
    call Google for every mentor every five minutes for no reason."""
    mentor = await make_bookable_mentor(db_engine, "next-age")
    first = dt.datetime.now(dt.UTC)
    await refresh(db_engine, now=first)

    early = MAX_AGE - JITTER - dt.timedelta(seconds=1)
    await refresh(db_engine, now=first + early)
    assert await computed_at(db_engine, mentor) == first

    later = first + MAX_AGE - JITTER + dt.timedelta(seconds=1)
    await refresh(db_engine, now=later)
    assert await computed_at(db_engine, mentor) == later


async def test_a_mentor_who_stops_being_bookable_leaves_the_card(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Bookability still decides who appears; a stored row does not bring a
    paused mentor back."""
    mentor = await make_bookable_mentor(db_engine, "next-paused")
    await refresh(db_engine)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE mentor_profiles SET listing_status = 'unlisted' WHERE user_id = :m"),
            {"m": mentor},
        )

    listed = [row["id"] for row in (await api_client.get(URL)).json()["data"]]

    assert str(mentor) not in listed


# --------------------------------------------------------------------------
# What does and does not mark a mentor stale
# --------------------------------------------------------------------------


async def state_after(
    client: httpx.AsyncClient, engine: AsyncEngine, mentor: UUID, *statements: str
) -> str:
    await refresh(engine)
    async with engine.begin() as conn:
        for sql in statements:
            await conn.execute(text(sql), {"m": mentor})
    return str((await card(client, mentor))["next_available_state"])


async def test_a_headline_edit_does_not_blank_the_card(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "next-bio")

    state = await state_after(
        api_client,
        db_engine,
        mentor,
        "UPDATE mentor_profiles SET headline = 'New headline' WHERE user_id = :m",
    )

    assert state == "open"


async def test_a_listing_change_does_mark_the_mentor(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The positive half of the column filter above: the columns that decide
    visibility still fire. Relisted straight after, so the mentor is still on
    the page to be read."""
    mentor = await make_bookable_mentor(db_engine, "next-listing")

    state = await state_after(
        api_client,
        db_engine,
        mentor,
        "UPDATE mentor_profiles SET listing_status = 'unlisted' WHERE user_id = :m",
        "UPDATE mentor_profiles SET listing_status = 'listed' WHERE user_id = :m",
    )

    assert state == "refreshing"


async def test_an_update_that_changes_nothing_does_not_blank_the_card(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "next-noop")

    state = await state_after(
        api_client,
        db_engine,
        mentor,
        "UPDATE availability_rules SET end_time = end_time WHERE mentor_user_id = :m",
    )

    assert state == "open"


async def test_a_dry_run_writes_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "next-dry")

    counts = await refresh(db_engine, dry_run=True)

    assert counts["refreshed"] >= 1
    assert (await card(api_client, mentor))["next_available_state"] == "refreshing"


async def test_settling_a_past_session_does_not_blank_the_card(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The hourly settle job moves yesterday's sessions to `completed`. That
    frees nothing ahead, so it must not blank every card it touches."""
    mentor = await make_bookable_mentor(db_engine, "next-settle")
    await add_session(db_engine, mentor, status="confirmed", days_ago=1)

    state = await state_after(
        api_client,
        db_engine,
        mentor,
        "UPDATE sessions SET status = 'completed' WHERE mentor_id = :m",
    )

    assert state == "open"


async def test_the_booking_deadline_is_the_slot_less_its_notice(
    db_engine: AsyncEngine,
) -> None:
    """Past `bookable_until` the notice window has closed on the slot, `/slots`
    no longer offers it, and the card must stop showing it."""
    mentor = await make_public_mentor(db_engine, "next-notice")
    await add_session_type(db_engine, mentor, notice=120)
    await add_availability(db_engine, mentor)
    await refresh(db_engine)

    async with db_engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT next_available_at, bookable_until FROM mentor_next_availability "
                    "WHERE mentor_user_id = :m"
                ),
                {"m": mentor},
            )
        ).one()

    assert row.next_available_at - row.bookable_until == dt.timedelta(minutes=120)


async def test_the_profile_and_user_triggers_watch_every_column_visibility_reads(
    db_engine: AsyncEngine,
) -> None:
    """Their triggers fire only for named columns, so a column `mentor_is_public()`
    starts reading must be named too — or a mentor who is unapproved or deleted
    keeps a vouched time. This fails the day the two lists diverge."""
    read = {
        (element.table.name, element.name)
        for clause in mentor_is_public()
        for element in visitors.iterate(clause)
        if isinstance(element, Column) and element.table.name in {"mentor_profiles", "users"}
    }
    assert read, "the predicate reads profile and user columns at all"

    async with db_engine.begin() as conn:
        watched = {
            (row.relname, row.attname)
            for row in await conn.execute(
                text(
                    "SELECT c.relname, a.attname FROM pg_trigger t "
                    "JOIN pg_class c ON c.oid = t.tgrelid "
                    "CROSS JOIN LATERAL unnest(t.tgattr::int2[]) AS k(attnum) "
                    "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum "
                    "WHERE t.tgname = 'trg_log_availability_change'"
                )
            )
        }

    assert read <= watched, read - watched


# --------------------------------------------------------------------------
# Failure and overlap
# --------------------------------------------------------------------------


async def test_one_mentor_failing_does_not_stop_the_run(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    broken = await make_bookable_mentor(db_engine, "next-broken")
    fine = await make_bookable_mentor(db_engine, "next-fine")

    counts = await refresh(db_engine, calendar=FakeCalendar(fails_for=broken))

    assert counts["failed"] == 1
    assert (await card(api_client, fine))["next_available_state"] == "open"
    assert (await card(api_client, broken))["next_available_state"] == "refreshing"


async def test_an_older_run_cannot_overwrite_a_newer_answer(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A QStash retry overlapping the run it retried: the one that started
    earlier finishes last. Its write is refused, and it must not clear the
    change it never saw."""
    mentor = await make_bookable_mentor(db_engine, "next-overlap")
    newer = dt.datetime.now(dt.UTC)
    await refresh(db_engine, now=newer)
    await add_session(db_engine, mentor, status="confirmed", days_ago=-3)

    counts = await refresh(db_engine, now=newer - dt.timedelta(minutes=2))

    assert counts["superseded"] == 1
    assert (await card(api_client, mentor))["next_available_state"] == "refreshing"


async def test_the_job_reader_raises_where_a_slot_read_fails_open(
    db_engine: AsyncEngine,
) -> None:
    """`/slots` answers one request from declared hours when Google is down.
    The job must not: stored, that answer would reach every viewer."""
    mentor = await make_bookable_mentor(db_engine, "next-google-down")
    await connect_calendar(db_engine, mentor)

    def unavailable(**_: Any) -> tuple[UtcInterval, ...]:
        raise VenueUnavailableError("rate limited")

    def reader(*, fail_open: bool) -> MentorFreeBusy:
        return MentorFreeBusy(
            client_id="cid",
            client_secret="gcs",  # noqa: S106
            key=KEY,
            reader=unavailable,
            fail_open=fail_open,
        )

    now = dt.datetime.now(dt.UTC)
    later = now + dt.timedelta(days=1)
    async with AsyncSession(db_engine) as session:
        assert await reader(fail_open=True).busy(session, mentor, now, later) == ()
        with pytest.raises(VenueUnavailableError):
            await reader(fail_open=False).busy(session, mentor, now, later)
