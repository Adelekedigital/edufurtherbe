"""`GET /me/mentee-stats`: a mentee's own home figures (decision 249).

**Two mentees and a dual-role caller wherever a scope is under test**, for the
reason `test_api_me_mentor_stats` gives: with one mentee in the database "this
mentee's rows" and "all rows" are the same set. Fixtures are distinguishable:
different durations and a repeated mentor, so a wrong formula gives a different
number.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.factories import (
    add_session,
    make_bookable_mentor,
    mentee_of,
    token_for,
)

from app.api.deps import PRIVATE_NO_STORE
from app.domain.mentee_stats import dashboard_windows
from app.infra.db.mentee_dashboard import mentee_dashboard
from conftest import add_user, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]

URL = "/api/v1/me/mentee-stats"

ZEROS = {
    "sessions_held": 0,
    "mentors_met": 0,
    "minutes_of_guidance": 0,
    "missed_sessions": 0,
}
EMPTY_BLOCK = {"completed_sessions": 0, "minutes": 0}


async def dashboard(client: httpx.AsyncClient, token: str) -> dict[str, Any]:
    response = await client.get(URL, headers=bearer(token))
    assert response.status_code == 200, response.text
    return dict(response.json())


def totals(body: dict[str, Any]) -> dict[str, Any]:
    return {key: body[key] for key in ZEROS}


def block(body: dict[str, Any], name: str) -> dict[str, Any]:
    return {key: body[name][key] for key in EMPTY_BLOCK}


async def first_mentee(engine: AsyncEngine, mentor: UUID, **knobs: Any) -> UUID:
    """A mentee made by their first session with ``mentor``."""
    return await mentee_of(engine, await add_session(engine, mentor, **knobs))


# --------------------------------------------------------------------------
# The figures
# --------------------------------------------------------------------------


async def test_each_figure_is_counted(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Held: 30 min (this month) and 45 min (last month) with one mentor, 60 min
    (four months ago) with another. Missed: one no-show of the mentee's own, 5
    days ago."""
    ada = await make_bookable_mentor(db_engine, "mdash-ada")
    bo = await make_bookable_mentor(db_engine, "mdash-bo")
    mentee = await first_mentee(db_engine, ada, minutes=30, days_ago=2)
    await add_session(db_engine, ada, mentee=mentee, minutes=45, days_ago=40)
    await add_session(db_engine, bo, mentee=mentee, minutes=60, days_ago=120)
    await add_session(
        db_engine,
        bo,
        mentee=mentee,
        status="no_show",
        attendance="attended",
        mentee_attendance="no_show",
        days_ago=5,
    )

    body = await dashboard(api_client, await token_for(db_engine, mentee))

    assert totals(body) == {
        "sessions_held": 3,
        "mentors_met": 2,
        "minutes_of_guidance": 135,
        "missed_sessions": 1,
    }
    assert body["window_days"] == 30
    assert body["missed_window_days"] == 90
    assert block(body, "current") == {"completed_sessions": 1, "minutes": 30}
    assert block(body, "previous") == {"completed_sessions": 1, "minutes": 45}


async def test_only_completed_sessions_are_held(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Cancelled, no-show, declined and upcoming sessions are not held, and
    their mentor is not one the mentee has met."""
    mentor = await make_bookable_mentor(db_engine, "mdash-held")
    other = await make_bookable_mentor(db_engine, "mdash-held-other")
    mentee = await first_mentee(db_engine, mentor, minutes=30, days_ago=2)
    for n, status in enumerate(("cancelled", "no_show", "declined"), start=3):
        await add_session(db_engine, other, mentee=mentee, status=status, days_ago=n)
    await add_session(db_engine, other, mentee=mentee, status="confirmed", days_ago=-2)

    body = await dashboard(api_client, await token_for(db_engine, mentee))

    assert totals(body)["sessions_held"] == 1
    assert totals(body)["mentors_met"] == 1
    assert totals(body)["minutes_of_guidance"] == 30
    assert block(body, "current") == {"completed_sessions": 1, "minutes": 30}


async def test_missed_counts_the_mentees_own_settled_no_shows(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A settled session the mentee was expected at and missed counts, even one
    stored `completed` (the column it is about, decision 101). The mentor's
    no-show, a cancelled session, and `pending` are not the mentee's miss."""
    mentor = await make_bookable_mentor(db_engine, "mdash-missed")
    mentee = await first_mentee(
        db_engine, mentor, status="no_show", attendance="attended", mentee_attendance="no_show"
    )
    await add_session(
        db_engine,
        mentor,
        mentee=mentee,
        status="completed",
        attendance="attended",
        mentee_attendance="no_show",
        days_ago=3,
    )
    await add_session(
        db_engine,
        mentor,
        mentee=mentee,
        status="no_show",
        attendance="no_show",
        mentee_attendance="attended",
        days_ago=4,
    )
    await add_session(
        db_engine,
        mentor,
        mentee=mentee,
        status="cancelled",
        attendance="pending",
        mentee_attendance="no_show",
        days_ago=5,
    )
    await add_session(
        db_engine,
        mentor,
        mentee=mentee,
        status="no_show",
        attendance="pending",
        mentee_attendance="pending",
        days_ago=6,
    )

    body = await dashboard(api_client, await token_for(db_engine, mentee))

    assert totals(body)["missed_sessions"] == 2


async def test_missed_reaches_back_90_days_only(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "mdash-missed-old")
    mentee = await first_mentee(
        db_engine,
        mentor,
        status="no_show",
        attendance="attended",
        mentee_attendance="no_show",
        days_ago=89,
    )
    await add_session(
        db_engine,
        mentor,
        mentee=mentee,
        status="no_show",
        attendance="attended",
        mentee_attendance="no_show",
        days_ago=91,
    )

    body = await dashboard(api_client, await token_for(db_engine, mentee))

    assert totals(body)["missed_sessions"] == 1


async def test_only_the_callers_sessions_as_a_mentee_count(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Another mentee's sessions and misses never reach the caller's figures,
    and nor do the caller's own sessions as a mentor: held, minutes and missed
    alike."""
    mentor = await make_bookable_mentor(db_engine, "mdash-mine")
    caller = await make_bookable_mentor(db_engine, "mdash-dual")
    await add_session(db_engine, mentor, mentee=caller, minutes=30, days_ago=2)
    # Another mentee of the same mentor: held and missed.
    stranger = await first_mentee(db_engine, mentor, minutes=90, days_ago=2)
    await add_session(
        db_engine,
        mentor,
        mentee=stranger,
        status="no_show",
        attendance="attended",
        mentee_attendance="no_show",
        days_ago=3,
    )
    # The caller as a mentor: a session held, and one they missed.
    pupil = await first_mentee(db_engine, caller, minutes=60, days_ago=2)
    await add_session(
        db_engine,
        caller,
        mentee=pupil,
        status="no_show",
        attendance="no_show",
        mentee_attendance="attended",
        days_ago=3,
    )

    body = await dashboard(api_client, await token_for(db_engine, caller))

    assert totals(body) == {
        "sessions_held": 1,
        "mentors_met": 1,
        "minutes_of_guidance": 30,
        "missed_sessions": 0,
    }
    assert block(body, "current") == {"completed_sessions": 1, "minutes": 30}


def assert_zeros(body: dict[str, Any]) -> None:
    assert totals(body) == ZEROS
    assert block(body, "current") == EMPTY_BLOCK
    assert block(body, "previous") == EMPTY_BLOCK


async def test_a_new_user_reads_zeros_not_an_error(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    async with db_engine.begin() as conn:
        user = await add_user(
            conn, f"mdash-new-{uuid4()}@example.test", primary_role="mentee", timezone="UTC"
        )

    assert_zeros(await dashboard(api_client, await token_for(db_engine, user)))


async def test_a_mentor_who_never_booked_reads_zeros(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Their sessions as a mentor are not received."""
    mentor = await make_bookable_mentor(db_engine, "mdash-mentor-only")
    await add_session(db_engine, mentor, minutes=30, days_ago=2)

    assert_zeros(await dashboard(api_client, await token_for(db_engine, mentor)))


async def test_the_windows_are_adjacent_month_long_and_utc(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "mdash-bounds")

    body = await dashboard(api_client, await token_for(db_engine, mentor))

    current, previous = body["current"], body["previous"]
    since, until = (dt.datetime.fromisoformat(current[k]) for k in ("since", "until"))
    assert until - since == dt.timedelta(days=30)
    assert previous["until"] == current["since"]
    assert until.utcoffset() == dt.timedelta(0)


# --------------------------------------------------------------------------
# The edges, at the store, where the clock is ours
# --------------------------------------------------------------------------

NOW = dt.datetime(2026, 6, 15, 12, tzinfo=dt.UTC)
WINDOWS = dashboard_windows(NOW)


async def store(engine: AsyncEngine, mentee: UUID) -> dict[str, Any]:
    async with async_sessionmaker(engine)() as session:
        return await mentee_dashboard(session, mentee, WINDOWS)


async def missed_at(engine: AsyncEngine, mentor: UUID, mentee: UUID, at: dt.datetime) -> None:
    await add_session(
        engine,
        mentor,
        mentee=mentee,
        status="no_show",
        attendance="attended",
        mentee_attendance="no_show",
        starts_at=at,
    )


async def test_since_is_inside_every_window(db_engine: AsyncEngine) -> None:
    mentor = await make_bookable_mentor(db_engine, "mdash-since")
    mentee = await first_mentee(db_engine, mentor, minutes=40, starts_at=WINDOWS.current.since)
    await add_session(
        db_engine, mentor, mentee=mentee, minutes=20, starts_at=WINDOWS.previous.since
    )
    await missed_at(db_engine, mentor, mentee, WINDOWS.missed.since)

    row = await store(db_engine, mentee)

    assert row["current"]["completed_sessions"] == 1
    assert row["current"]["minutes"] == 40
    assert row["previous"]["minutes"] == 20
    assert row["missed_sessions"] == 1


async def test_until_is_outside_every_window(db_engine: AsyncEngine) -> None:
    """A session starting at `now` is in neither month, and a no-show then is
    not yet in the missed window. Held all time counts it: all time has no
    edge."""
    mentor = await make_bookable_mentor(db_engine, "mdash-until")
    mentee = await first_mentee(db_engine, mentor, minutes=40, starts_at=WINDOWS.current.until)
    await add_session(
        db_engine,
        mentor,
        mentee=mentee,
        minutes=20,
        starts_at=WINDOWS.previous.since - dt.timedelta(microseconds=1),
    )
    await missed_at(db_engine, mentor, mentee, WINDOWS.missed.until)

    row = await store(db_engine, mentee)

    assert row["current"] == {**EMPTY_BLOCK, **WINDOWS.current._asdict()}
    assert row["previous"] == {**EMPTY_BLOCK, **WINDOWS.previous._asdict()}
    assert row["missed_sessions"] == 0
    assert row["sessions_held"] == 2


async def test_everything_is_read_in_one_statement(db_engine: AsyncEngine) -> None:
    """One statement is one snapshot: a session settling mid-read cannot land
    in the all-time count and be missing from this month's."""
    mentor = await make_bookable_mentor(db_engine, "mdash-snapshot")
    mentee = await first_mentee(db_engine, mentor, minutes=20, starts_at=NOW - dt.timedelta(days=2))
    await add_session(
        db_engine, mentor, mentee=mentee, minutes=50, starts_at=NOW - dt.timedelta(days=35)
    )
    statements: list[str] = []

    def record(*args: Any) -> None:
        statements.append(args[2])

    event.listen(db_engine.sync_engine, "before_cursor_execute", record)
    try:
        row = await store(db_engine, mentee)
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", record)

    assert len(statements) == 1, statements
    assert (row["current"]["minutes"], row["previous"]["minutes"]) == (20, 50)
    assert row["minutes_of_guidance"] == 70


# --------------------------------------------------------------------------
# The contract
# --------------------------------------------------------------------------


async def test_it_needs_a_token(api_client: httpx.AsyncClient) -> None:
    assert (await api_client.get(URL)).status_code == 401


async def test_nobody_else_caches_it(api_client: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    mentor = await make_bookable_mentor(db_engine, "mdash-cache")

    response = await api_client.get(URL, headers=bearer(await token_for(db_engine, mentor)))

    assert response.headers["Cache-Control"] == PRIVATE_NO_STORE
