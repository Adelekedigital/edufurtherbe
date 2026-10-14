"""`GET /me/mentor-stats/monthly` — a mentor's figures per month (decision 247).

The figures are decision 246's, bucketed by UTC calendar month, so the edges
are tested at the store with a fixed clock and the contract through the API.
**Two mentors wherever a scope is under test**, for the reason
`test_api_me_mentor_stats` gives.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.factories import (
    add_session,
    cancelled,
    ended_by,
    make_bookable_mentor,
    token_for,
)

from app.api.deps import PRIVATE_NO_STORE
from app.domain.mentor_stats import month_windows
from app.infra.db.mentor_dashboard import mentor_monthly_stats
from conftest import bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]

URL = "/api/v1/me/mentor-stats/monthly"

ZEROS = {
    "completed_sessions": 0,
    "missed_by_mentor": 0,
    "cancelled_by_mentor": 0,
    "cancelled_by_mentee": 0,
    "cancelled_by_other": 0,
}

NOW = dt.datetime(2026, 6, 15, 12, tzinfo=dt.UTC)
APRIL, MAY, JUNE = month_windows(NOW, 3)


def figures(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in ZEROS}


async def series(engine: AsyncEngine, mentor: UUID) -> list[dict[str, Any]]:
    async with async_sessionmaker(engine)() as session:
        return await mentor_monthly_stats(session, mentor, [APRIL, MAY, JUNE])


# --------------------------------------------------------------------------
# The figures, at the store, where the clock is ours
# --------------------------------------------------------------------------


async def test_each_month_counts_its_own_sessions(db_engine: AsyncEngine) -> None:
    """Held and missed by `starts_at`; cancellations split by who made them;
    an admin acting is not the mentor even under the mentor's id; a decline is
    not a cancellation; a month with nothing in it is still there."""
    mentor = await make_bookable_mentor(db_engine, "monthly-figures")
    in_may = MAY.since + dt.timedelta(days=3)
    await add_session(db_engine, mentor, starts_at=APRIL.since + dt.timedelta(days=9))
    await add_session(db_engine, mentor, starts_at=in_may)
    await add_session(db_engine, mentor, starts_at=in_may + dt.timedelta(days=1))
    await add_session(db_engine, mentor, status="no_show", attendance="no_show", starts_at=in_may)
    await cancelled(db_engine, mentor, "mentor", starts_at=in_may, at=in_may)
    await cancelled(db_engine, mentor, "mentor", starts_at=in_may, at=in_may)
    await cancelled(db_engine, mentor, "mentee", starts_at=in_may, at=in_may)
    await cancelled(db_engine, mentor, "system", starts_at=in_may, at=in_may)
    await cancelled(db_engine, mentor, "admin", starts_at=in_may, at=in_may)
    declined = await add_session(
        db_engine, mentor, status="declined", attendance="pending", starts_at=in_may
    )
    await ended_by(db_engine, declined, mentor, "declined", at=in_may)

    rows = await series(db_engine, mentor)

    assert [(row["since"], row["until"]) for row in rows] == [APRIL, MAY, JUNE]
    assert [figures(row) for row in rows] == [
        {**ZEROS, "completed_sessions": 1},
        {
            "completed_sessions": 2,
            "missed_by_mentor": 1,
            "cancelled_by_mentor": 2,
            "cancelled_by_mentee": 1,
            "cancelled_by_other": 2,
        },
        ZEROS,
    ]


async def test_a_month_includes_its_first_instant(db_engine: AsyncEngine) -> None:
    mentor = await make_bookable_mentor(db_engine, "monthly-since")
    await add_session(db_engine, mentor, starts_at=MAY.since)
    await add_session(
        db_engine, mentor, status="no_show", attendance="no_show", starts_at=MAY.since
    )
    await cancelled(db_engine, mentor, "mentee", starts_at=JUNE.since, at=MAY.since)

    rows = await series(db_engine, mentor)

    assert figures(rows[1]) == {
        **ZEROS,
        "completed_sessions": 1,
        "missed_by_mentor": 1,
        "cancelled_by_mentee": 1,
    }
    assert figures(rows[0]) == ZEROS


async def test_a_month_excludes_its_last_instant(db_engine: AsyncEngine) -> None:
    """`until` is the next month's `since`: on the edge is June's, not May's."""
    mentor = await make_bookable_mentor(db_engine, "monthly-until")
    await add_session(db_engine, mentor, starts_at=MAY.until)
    await add_session(
        db_engine, mentor, status="no_show", attendance="no_show", starts_at=MAY.until
    )
    await cancelled(db_engine, mentor, "mentor", starts_at=MAY.since, at=MAY.until)

    rows = await series(db_engine, mentor)

    assert figures(rows[1]) == ZEROS
    assert figures(rows[2]) == {
        **ZEROS,
        "completed_sessions": 1,
        "missed_by_mentor": 1,
        "cancelled_by_mentor": 1,
    }


async def test_a_cancellation_counts_in_the_month_it_was_made(db_engine: AsyncEngine) -> None:
    """Cancelled in April for a June session is April's; cancelled before the
    series for a May session is in no month."""
    mentor = await make_bookable_mentor(db_engine, "monthly-cancel-when")
    await cancelled(
        db_engine,
        mentor,
        "mentor",
        starts_at=JUNE.since + dt.timedelta(days=2),
        at=APRIL.since + dt.timedelta(days=5),
    )
    await cancelled(
        db_engine,
        mentor,
        "mentee",
        starts_at=MAY.since + dt.timedelta(days=2),
        at=APRIL.since - dt.timedelta(days=5),
    )

    rows = await series(db_engine, mentor)

    assert [figures(row) for row in rows] == [
        {**ZEROS, "cancelled_by_mentor": 1},
        ZEROS,
        ZEROS,
    ]


async def test_only_the_callers_sessions_count(db_engine: AsyncEngine) -> None:
    """Another mentor's sessions and cancellations never reach the caller's
    months, nor do sessions the caller received or cancelled as a mentee."""
    mentor = await make_bookable_mentor(db_engine, "monthly-mine")
    other = await make_bookable_mentor(db_engine, "monthly-theirs")
    in_may = MAY.since + dt.timedelta(days=3)
    await add_session(db_engine, mentor, starts_at=in_may)
    await add_session(db_engine, other, starts_at=in_may)
    await add_session(db_engine, other, status="no_show", attendance="no_show", starts_at=in_may)
    await cancelled(db_engine, other, "mentor", starts_at=in_may, at=in_may)
    await cancelled(db_engine, other, "system", starts_at=in_may, at=in_may)
    await add_session(db_engine, other, mentee=mentor, starts_at=in_may)
    as_mentee = await add_session(
        db_engine, other, status="cancelled", attendance="pending", mentee=mentor, starts_at=in_may
    )
    await ended_by(db_engine, as_mentee, mentor, "cancelled", at=in_may)

    rows = await series(db_engine, mentor)

    assert [figures(row) for row in rows] == [ZEROS, {**ZEROS, "completed_sessions": 1}, ZEROS]


async def test_every_month_is_read_in_one_statement(db_engine: AsyncEngine) -> None:
    mentor = await make_bookable_mentor(db_engine, "monthly-snapshot")
    statements: list[str] = []

    def record(*args: Any) -> None:
        statements.append(args[2])

    event.listen(db_engine.sync_engine, "before_cursor_execute", record)
    try:
        rows = await series(db_engine, mentor)
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", record)

    assert len(statements) == 1, statements
    assert len(rows) == 3


# --------------------------------------------------------------------------
# The contract
# --------------------------------------------------------------------------


async def test_the_series_is_every_month_oldest_first(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Twelve months by default, adjacent, UTC, ending with this month, with
    the empty ones present so a chart has no gaps."""
    mentor = await make_bookable_mentor(db_engine, "monthly-api")
    other = await make_bookable_mentor(db_engine, "monthly-api-other")
    months = month_windows(dt.datetime.now(dt.UTC), 12)
    held = months[-3].since + dt.timedelta(days=1)
    await add_session(db_engine, mentor, starts_at=held)
    await add_session(db_engine, other, starts_at=held)

    response = await api_client.get(URL, headers=bearer(await token_for(db_engine, mentor)))

    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == PRIVATE_NO_STORE
    body = response.json()
    assert body["months"] == 12
    assert [
        (dt.datetime.fromisoformat(m["since"]), dt.datetime.fromisoformat(m["until"]))
        for m in body["series"]
    ] == [tuple(window) for window in months]
    assert body["series"][-1]["month"] == months[-1].since.strftime("%Y-%m")
    assert [m["completed_sessions"] for m in body["series"]] == [0] * 9 + [1, 0, 0]
    assert dt.datetime.fromisoformat(body["series"][0]["since"]).utcoffset() == dt.timedelta(0)


async def test_a_non_mentor_reads_zeros_not_an_error(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    async with db_engine.begin() as conn:
        user = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, primary_role, timezone) "
                    "VALUES (:e, 'mentee', 'UTC') RETURNING id"
                ),
                {"e": f"plain-monthly-{uuid4()}@example.test"},
            )
        ).scalar_one()

    response = await api_client.get(
        URL + "?months=2", headers=bearer(await token_for(db_engine, user))
    )

    assert response.status_code == 200, response.text
    assert [figures(m) for m in response.json()["series"]] == [ZEROS, ZEROS]


@pytest.mark.parametrize(("months", "code"), [(0, 422), (1, 200), (24, 200), (25, 422)])
async def test_months_is_bounded(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, months: int, code: int
) -> None:
    mentor = await make_bookable_mentor(db_engine, f"monthly-bound-{months}")

    response = await api_client.get(
        f"{URL}?months={months}", headers=bearer(await token_for(db_engine, mentor))
    )

    assert response.status_code == code, response.text
    if code == 200:
        assert len(response.json()["series"]) == months


async def test_it_needs_a_token(api_client: httpx.AsyncClient) -> None:
    assert (await api_client.get(URL)).status_code == 401
