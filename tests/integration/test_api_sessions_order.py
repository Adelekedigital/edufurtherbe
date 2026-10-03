"""`GET /users/{id}/sessions?order=asc` — soonest first, for an Upcoming tab.

The list stays newest first by default. `asc` turns it around **before** the
cursor is cut, so the next session is on page one rather than the last page. A
cursor remembers its direction: replayed under the other one it is a `422`, not
a page from the wrong place.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_sessions import make_session, pair, sessions_url

from app.api.schemas.common import encode_cursor
from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

BASE = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)


async def walk(client: httpx.AsyncClient, url: str, auth_id: object, query: str) -> list[str]:
    """Every id across every page, in the order served."""
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(6):  # bounded, so a cursor that never advances fails rather than hangs
        page = await client.get(
            f"{url}?{query}" + (f"&cursor={cursor}" if cursor else ""),
            headers=bearer(api_token(auth_id)),  # type: ignore[arg-type]
        )
        assert page.status_code == 200, page.text
        seen.extend(row["id"] for row in page.json()["data"])
        cursor = page.json()["next_cursor"]
        if cursor is None:
            return seen
    raise AssertionError("paging never terminated")


async def test_asc_pages_soonest_first_with_no_repeats_or_gaps(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Two at a time over six, with **three** sessions at one instant, so the tie
    straddles the first page boundary and the id half of the cursor is
    load-bearing in this direction too — comparing on `starts_at` alone skips
    the third."""
    mentor, _, mentee, auth = await pair(db_engine, "o-asc")
    later = [
        await make_session(db_engine, mentor, mentee, starts_at=BASE + timedelta(days=d))
        for d in (3, 1, 2)
    ]
    tied = sorted(
        [str(await make_session(db_engine, mentor, mentee, starts_at=BASE)) for _ in range(3)]
    )

    served = await walk(api_client, sessions_url(mentee), auth, "order=asc&limit=2")

    assert served == [*tied, str(later[1]), str(later[2]), str(later[0])]


async def test_desc_stays_the_default_and_newest_first(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, _, mentee, auth = await pair(db_engine, "o-desc")
    made = [
        str(await make_session(db_engine, mentor, mentee, starts_at=BASE + timedelta(days=d)))
        for d in range(3)
    ]

    assert await walk(api_client, sessions_url(mentee), auth, "limit=2") == made[::-1]
    assert await walk(api_client, sessions_url(mentee), auth, "order=desc&limit=2") == made[::-1]


async def test_a_cursor_minted_before_order_existed_still_pages_newest_first(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Clients hold untagged cursors from before this change; desc is still theirs."""
    mentor, _, mentee, auth = await pair(db_engine, "o-old")
    first, second, third = [
        await make_session(db_engine, mentor, mentee, starts_at=BASE + timedelta(days=d))
        for d in range(3)
    ]
    old = encode_cursor((BASE + timedelta(days=2)).isoformat(), third)

    page = await api_client.get(
        f"{sessions_url(mentee)}?cursor={old}", headers=bearer(api_token(auth))
    )

    assert page.status_code == 200, page.text
    assert [row["id"] for row in page.json()["data"]] == [str(second), str(first)]


@pytest.mark.parametrize(("minted", "replayed"), [("asc", "desc"), ("desc", "asc")])
async def test_a_cursor_replayed_in_the_other_direction_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, minted: str, replayed: str
) -> None:
    mentor, _, mentee, auth = await pair(db_engine, f"o-cross-{minted}")
    for d in range(3):
        await make_session(db_engine, mentor, mentee, starts_at=BASE + timedelta(days=d))
    first = await api_client.get(
        f"{sessions_url(mentee)}?order={minted}&limit=1", headers=bearer(api_token(auth))
    )
    cursor = first.json()["next_cursor"]
    assert cursor

    page = await api_client.get(
        f"{sessions_url(mentee)}?order={replayed}&limit=1&cursor={cursor}",
        headers=bearer(api_token(auth)),
    )

    assert page.status_code == 422, page.text


async def test_an_unknown_order_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, _, mentee, auth = await pair(db_engine, "o-bad")

    page = await api_client.get(
        f"{sessions_url(mentee)}?order=sideways", headers=bearer(api_token(auth))
    )

    assert page.status_code == 422, page.text


async def test_upcoming_is_confirmed_from_today_soonest_first(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The Bookings Upcoming tab: the next confirmed session leads page one."""
    mentor, _, mentee, auth = await pair(db_engine, "o-upcoming")
    await make_session(db_engine, mentor, mentee, starts_at=BASE - timedelta(days=2))
    await make_session(
        db_engine, mentor, mentee, status="cancelled", starts_at=BASE + timedelta(days=1)
    )
    soonest = await make_session(
        db_engine, mentor, mentee, status="confirmed", starts_at=BASE + timedelta(days=2)
    )
    next_up = await make_session(
        db_engine, mentor, mentee, status="confirmed", starts_at=BASE + timedelta(days=4)
    )

    served = await walk(
        api_client,
        sessions_url(mentee),
        auth,
        f"order=asc&status=confirmed&from={BASE.date().isoformat()}&limit=1",
    )

    assert served == [str(soonest), str(next_up)]
