"""`GET /users/{id}/sessions` narrowed by date and status — the month view.

`from` and `to` are the **caller's** calendar dates (`to` exclusive), turned into
instants at the caller's local midnight, so a session at 23:30 on the last
evening of a month lands in that month for the person looking at it. `status`
repeats. Ordering and the keyset cursor are the unfiltered list's, applied
inside the filter.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_sessions import make_session, pair, sessions_url

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def set_zone(engine: AsyncEngine, user_id: UUID, zone: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET timezone = :z WHERE id = :u"), {"z": zone, "u": user_id}
        )


async def listed(
    client: httpx.AsyncClient, user_id: UUID, auth: UUID, query: str
) -> httpx.Response:
    return await client.get(f"{sessions_url(user_id)}?{query}", headers=bearer(api_token(auth)))


def ids(response: httpx.Response) -> set[str]:
    return {row["id"] for row in response.json()["data"]}


async def test_a_month_holds_only_sessions_starting_in_it_by_local_midnight(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Lagos is UTC+1: 30 Sep 23:30 UTC is already 1 Oct there, and 31 Oct
    23:30 UTC is 1 Nov there — the boundaries move with the caller's zone."""
    mentor, _, mentee, auth = await pair(db_engine, "f-lagos")
    inside_first = await make_session(
        db_engine, mentor, mentee, starts_at=datetime(2026, 9, 30, 23, 30, tzinfo=UTC)
    )
    before = await make_session(
        db_engine, mentor, mentee, starts_at=datetime(2026, 9, 30, 22, 30, tzinfo=UTC)
    )
    inside_last = await make_session(
        db_engine, mentor, mentee, starts_at=datetime(2026, 10, 31, 22, 30, tzinfo=UTC)
    )
    after = await make_session(
        db_engine, mentor, mentee, starts_at=datetime(2026, 10, 31, 23, 30, tzinfo=UTC)
    )

    response = await listed(api_client, mentee, auth, "from=2026-10-01&to=2026-11-01")

    assert response.status_code == 200, response.text
    assert ids(response) == {str(inside_first), str(inside_last)}
    assert str(before) not in ids(response) and str(after) not in ids(response)


async def test_the_window_follows_a_zone_west_of_utc_too(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Los Angeles is UTC-7 in October: 1 Oct 06:30 UTC is still 30 Sep there."""
    mentor, _, mentee, auth = await pair(db_engine, "f-la")
    await set_zone(db_engine, mentee, "America/Los_Angeles")
    still_september = await make_session(
        db_engine, mentor, mentee, starts_at=datetime(2026, 10, 1, 6, 30, tzinfo=UTC)
    )
    first_of_october = await make_session(
        db_engine, mentor, mentee, starts_at=datetime(2026, 10, 1, 7, 30, tzinfo=UTC)
    )

    response = await listed(api_client, mentee, auth, "from=2026-10-01&to=2026-10-02")

    assert response.status_code == 200, response.text
    assert ids(response) == {str(first_of_october)}
    assert str(still_september) not in ids(response)


async def test_to_is_exclusive(api_client: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    """A session starting at the exact local midnight `to` names is outside."""
    mentor, _, mentee, auth = await pair(db_engine, "f-excl")
    # 23:00 UTC on the 4th is 00:00 on the 5th in Lagos (UTC+1).
    on_the_fifth = await make_session(
        db_engine, mentor, mentee, starts_at=datetime(2026, 10, 4, 23, 0, tzinfo=UTC)
    )
    on_the_fourth = await make_session(
        db_engine, mentor, mentee, starts_at=datetime(2026, 10, 4, 9, 0, tzinfo=UTC)
    )

    response = await listed(api_client, mentee, auth, "from=2026-10-01&to=2026-10-05")

    assert ids(response) == {str(on_the_fourth)}
    assert str(on_the_fifth) not in ids(response)


async def test_from_includes_a_session_at_its_local_midnight(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The inclusive edge: 23:00 UTC on the 30th is 00:00 on the 1st in Lagos."""
    mentor, _, mentee, auth = await pair(db_engine, "f-incl")
    at_midnight = await make_session(
        db_engine, mentor, mentee, starts_at=datetime(2026, 9, 30, 23, 0, tzinfo=UTC)
    )
    just_before = await make_session(
        db_engine, mentor, mentee, starts_at=datetime(2026, 9, 30, 22, 59, tzinfo=UTC)
    )

    response = await listed(api_client, mentee, auth, "from=2026-10-01&to=2026-10-05")

    assert ids(response) == {str(at_midnight)}
    assert str(just_before) not in ids(response)


async def test_status_repeats_and_keeps_only_those(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, _, mentee, auth = await pair(db_engine, "f-status")
    confirmed = await make_session(
        db_engine,
        mentor,
        mentee,
        status="confirmed",
        starts_at=datetime(2026, 10, 6, 9, 0, tzinfo=UTC),
    )
    pending = await make_session(
        db_engine,
        mentor,
        mentee,
        status="pending_mentor_approval",
        starts_at=datetime(2026, 10, 7, 9, 0, tzinfo=UTC),
    )
    cancelled = await make_session(
        db_engine, mentor, mentee, starts_at=datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
    )

    response = await listed(
        api_client, mentee, auth, "status=confirmed&status=pending_mentor_approval"
    )

    assert response.status_code == 200, response.text
    assert ids(response) == {str(confirmed), str(pending)}
    assert str(cancelled) not in ids(response)


async def test_filters_page_with_the_cursor(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, _, mentee, auth = await pair(db_engine, "f-page")
    inside = {
        str(
            await make_session(
                db_engine, mentor, mentee, starts_at=datetime(2026, 10, day, 9, 0, tzinfo=UTC)
            )
        )
        for day in (3, 4, 5)
    }
    await make_session(db_engine, mentor, mentee, starts_at=datetime(2026, 11, 3, 9, 0, tzinfo=UTC))
    query = "from=2026-10-01&to=2026-11-01&limit=2"

    first = await listed(api_client, mentee, auth, query)
    second = await listed(api_client, mentee, auth, f"{query}&cursor={first.json()['next_cursor']}")

    assert first.json()["next_cursor"]
    assert second.json()["next_cursor"] is None
    assert ids(first) | ids(second) == inside
    assert ids(first) & ids(second) == set()


@pytest.mark.parametrize(
    "query",
    [
        "from=2026-10-05&to=2026-10-05",
        "from=2026-10-06&to=2026-10-05",
        "status=not_a_status",
        "from=0001-01-01",
        "to=9999-12-31",
    ],
    ids=["empty-range", "inverted-range", "unknown-status", "year-one", "year-9999"],
)
async def test_an_unusable_filter_is_a_client_error(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, query: str
) -> None:
    _, _, mentee, auth = await pair(db_engine, f"f-bad-{abs(hash(query))}")

    response = await listed(api_client, mentee, auth, query)

    assert response.status_code == 422, response.text


async def test_no_filters_lists_everything_as_before(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, _, mentee, auth = await pair(db_engine, "f-none")
    every = {
        str(await make_session(db_engine, mentor, mentee, starts_at=moment))
        for moment in (
            datetime(2025, 1, 1, 9, 0, tzinfo=UTC),
            datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
        )
    }

    response = await listed(api_client, mentee, auth, "limit=10")

    assert ids(response) == every
