"""`GET /mentors?available_within_days=N`: mentors with a vouched free time soon.

**It reads the card's stored time, through the card's own gate.** A mentor
matches only while `next_available_state` would say `open` (decision 175) and
the stored time is before `now() + N x 24 hours`, by the database's clock. So
every way the card withholds a time — never refreshed, a change since the
refresh, a slot past its booking deadline, nothing free, not taking bookings —
also keeps the mentor out of the filter. Each test below holds one of those
beside a mentor who does match, so an over-tight filter fails as surely as a
loose one.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.integration.factories import make_bookable_mentor, make_public_mentor
from tests.integration.test_api_mentor_search import give_offering, set_headline, walk
from tests.integration.test_api_mentors_personalised import a_mentee, listed

from app.infra.db.mentor_search_store import count_mentors
from conftest import PLATFORM_WINDOW

pytestmark = [pytest.mark.db, pytest.mark.anyio]

URL = "/api/v1/mentors"
TESTS = "test-preparation"


async def store_next_free(
    engine: AsyncEngine,
    mentor: UUID,
    at: dt.datetime | None,
    *,
    until: dt.datetime | None = None,
    changed_since: bool = False,
) -> None:
    """Write the refresh job's answer for `mentor`: free at `at` (`None` for
    nothing free), bookable until `until` (an hour before `at` by default).

    The change log is cleared as a refresh would, unless `changed_since`, which
    leaves one row behind: something changed after the refresh looked.
    """
    if until is None and at is not None:
        until = at - dt.timedelta(hours=1)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO mentor_next_availability "
                "(mentor_user_id, next_available_at, bookable_until, computed_at) "
                "VALUES (:u, :at, :until, now())"
            ),
            {"u": mentor, "at": at, "until": until},
        )
        await conn.execute(
            text("DELETE FROM mentor_availability_changes WHERE mentor_user_id = :u"),
            {"u": mentor},
        )
        if changed_since:
            await conn.execute(
                text("INSERT INTO mentor_availability_changes (mentor_user_id) VALUES (:u)"),
                {"u": mentor},
            )


def soon(days: float) -> dt.datetime:
    return (dt.datetime.now(dt.UTC) + dt.timedelta(days=days)).replace(microsecond=0)


async def ids(client: httpx.AsyncClient, query: str, **kwargs: Any) -> set[str]:
    response = await client.get(f"{URL}?{query}", **kwargs)
    assert response.status_code == 200, response.text
    return {row["id"] for row in response.json()["data"]}


async def test_lists_only_mentors_free_inside_the_window(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    inside = await make_bookable_mentor(db_engine, "within-inside")
    outside = await make_bookable_mentor(db_engine, "within-outside")
    await store_next_free(db_engine, inside, soon(2))
    await store_next_free(db_engine, outside, soon(9))

    assert await ids(api_client, "available_within_days=7") == {str(inside)}
    assert await ids(api_client, "available_within_days=10") == {str(inside), str(outside)}
    # Without the parameter nobody is filtered out.
    assert await ids(api_client, "") == {str(inside), str(outside)}


async def test_the_card_shows_the_time_the_filter_matched(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "within-card")
    at = soon(1)
    await store_next_free(db_engine, mentor, at)

    body = (await api_client.get(f"{URL}?available_within_days=1")).json()

    [row] = body["data"]
    assert row["next_available_state"] == "open"
    assert dt.datetime.fromisoformat(row["next_available_at"]) == at


@pytest.mark.parametrize(
    ("why", "at_days", "until_days", "changed_since"),
    [
        # `refreshing`: something changed after the refresh looked.
        ("changed since the refresh", 1, None, True),
        # `refreshing`: the slot's booking deadline has passed.
        ("past its booking deadline", 1, -0.01, False),
        # `none`: the refresh found nothing free.
        ("nothing free", None, None, False),
    ],
)
async def test_a_time_the_card_does_not_vouch_for_never_matches(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    why: str,
    at_days: float | None,
    until_days: float | None,
    changed_since: bool,
) -> None:
    vouched = await make_bookable_mentor(db_engine, "within-vouched")
    unvouched = await make_bookable_mentor(db_engine, "within-unvouched")
    await store_next_free(db_engine, vouched, soon(1))
    await store_next_free(
        db_engine,
        unvouched,
        None if at_days is None else soon(at_days),
        until=None if until_days is None else soon(until_days),
        changed_since=changed_since,
    )

    assert await ids(api_client, "available_within_days=14") == {str(vouched)}, why


async def test_a_mentor_never_refreshed_does_not_match(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """No stored row reads `refreshing`: not matched until a refresh writes one."""
    refreshed = await make_bookable_mentor(db_engine, "within-refreshed")
    await make_bookable_mentor(db_engine, "within-never")
    await store_next_free(db_engine, refreshed, soon(1))

    assert await ids(api_client, "available_within_days=14") == {str(refreshed)}


async def test_a_mentor_not_taking_bookings_does_not_match_on_a_left_over_time(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    bookable = await make_bookable_mentor(db_engine, "within-bookable")
    not_bookable = await make_public_mentor(db_engine, "within-not-bookable")
    await store_next_free(db_engine, bookable, soon(1))
    await store_next_free(db_engine, not_bookable, soon(1))

    assert await ids(api_client, "") == {str(bookable), str(not_bookable)}
    assert await ids(api_client, "available_within_days=14") == {str(bookable)}


async def _matches_at(engine: AsyncEngine, mentor: UUID, offset: dt.timedelta) -> int:
    """`total` for a 7-day window with the stored time at `now() + 168 hours +
    offset`, written and counted in one transaction so `now()` is one instant."""
    async with AsyncSession(engine) as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO mentor_next_availability "
                "(mentor_user_id, next_available_at, bookable_until, computed_at) "
                "VALUES (:u, now() + interval '168 hours' + :o, "
                "        now() + interval '1 hour', now())"
            ),
            {"u": mentor, "o": offset},
        )
        await session.execute(
            text("DELETE FROM mentor_availability_changes WHERE mentor_user_id = :u"),
            {"u": mentor},
        )
        total = await count_mentors(session, window=PLATFORM_WINDOW, within_days=7)
        await session.rollback()
    return total


async def test_the_window_ends_just_before_n_times_24_hours_from_now(
    db_engine: AsyncEngine,
) -> None:
    """Half-open: a time exactly `N x 24` hours from the database's `now()` is
    outside, the microsecond before it inside."""
    mentor = await make_bookable_mentor(db_engine, "within-edge")

    assert await _matches_at(db_engine, mentor, -dt.timedelta(microseconds=1)) == 1
    assert await _matches_at(db_engine, mentor, dt.timedelta(0)) == 0


async def test_combines_with_the_offering_filter(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    both = await make_bookable_mentor(db_engine, "within-offer-both")
    soon_only = await make_bookable_mentor(db_engine, "within-offer-soon")
    offer_only = await make_bookable_mentor(db_engine, "within-offer-offer")
    for mentor in (both, offer_only):
        await give_offering(db_engine, mentor, TESTS)
    await store_next_free(db_engine, both, soon(1))
    await store_next_free(db_engine, soon_only, soon(1))
    await store_next_free(db_engine, offer_only, soon(20))

    assert await ids(api_client, f"offering={TESTS}") == {str(both), str(offer_only)}
    assert await ids(api_client, f"offering={TESTS}&available_within_days=7") == {str(both)}


async def test_combines_with_a_search(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    both = await make_bookable_mentor(db_engine, "within-q-both")
    soon_only = await make_bookable_mentor(db_engine, "within-q-soon")
    match_only = await make_bookable_mentor(db_engine, "within-q-match")
    for mentor in (both, match_only):
        await set_headline(db_engine, mentor, "Chemistry tutor")
    for mentor in (both, soon_only):
        await store_next_free(db_engine, mentor, soon(1))

    assert await ids(api_client, "q=chemistry") == {str(both), str(match_only)}
    assert await ids(api_client, "q=chemistry&available_within_days=7") == {str(both)}


async def test_applies_to_every_page_of_the_goal_ranked_list(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Three matches at two a page, so the goal cursor's second page is read
    with the filter too."""
    free = set()
    for n in range(3):
        mentor = await make_bookable_mentor(db_engine, f"within-goal-free-{n}")
        await give_offering(db_engine, mentor, TESTS)
        await store_next_free(db_engine, mentor, soon(1))
        free.add(str(mentor))
    busy = await make_bookable_mentor(db_engine, "within-goal-busy")
    await give_offering(db_engine, busy, TESTS)
    _, headers = await a_mentee(db_engine, "within-goal", goals=(TESTS,))

    assert set(await listed(api_client, headers)) == free | {str(busy)}
    order = await listed(api_client, headers, "&available_within_days=7")
    assert len(order) == len(set(order)) == 3
    assert set(order) == free


@pytest.mark.parametrize("query", ["", "q=Ada&", f"offering={TESTS}&"])
async def test_the_total_counts_what_the_pages_list_with_the_filter(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, query: str
) -> None:
    """Walked page by page at one row a page, so the browse cursor, the search
    offset and `total` are each held to the same set."""
    for n in range(4):
        mentor = await make_bookable_mentor(db_engine, f"within-total-{n}")
        await give_offering(db_engine, mentor, TESTS)
        if n:
            await store_next_free(db_engine, mentor, soon(n))

    total, seen = await walk(api_client, f"{query}available_within_days=7")

    assert total == len(seen) == len(set(seen)) == 3


@pytest.mark.parametrize("days", [1, 14])
async def test_the_ends_of_the_range_are_accepted(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, days: int
) -> None:
    mentor = await make_bookable_mentor(db_engine, f"within-range-{days}")
    await store_next_free(db_engine, mentor, soon(0.5))

    assert await ids(api_client, f"available_within_days={days}") == {str(mentor)}


@pytest.mark.parametrize("value", ["0", "15", "-1", "seven", "1.5"])
async def test_a_value_outside_1_to_14_is_a_422(api_client: httpx.AsyncClient, value: str) -> None:
    response = await api_client.get(f"{URL}?available_within_days={value}")

    assert response.status_code == 422, response.text
