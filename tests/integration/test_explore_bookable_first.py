"""On Explore, a mentor taking bookings comes before one who is not (#220).

Owner decision 2026-09-29, on #298: a listed mentor with nothing bookable stays
on Explore (#219) but ranks below every bookable one. Inside each group the
order is what it was — newest first, the search rank, or the goal ranking — and
`total` counts both groups.
"""

from __future__ import annotations

import base64
from uuid import UUID

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import make_bookable_mentor, make_public_mentor
from tests.integration.test_api_mentor_search import set_headline
from tests.integration.test_api_mentors import give_offering
from tests.integration.test_api_mentors_personalised import INTERVIEW, a_mentee, a_mentor

from app.api.schemas import common

pytestmark = [pytest.mark.db, pytest.mark.anyio]

URL = "/api/v1/mentors"


async def walk(
    client: httpx.AsyncClient, query: str = "", headers: dict[str, str] | None = None
) -> list[str]:
    """Every card id across every page, one per page, in order."""
    ids: list[str] = []
    cursor: str | None = None
    for _ in range(20):  # bounded: a cursor that never advances fails, not hangs
        page = f"{URL}?limit=1{query}" + (f"&cursor={cursor}" if cursor else "")
        response = await client.get(page, headers=headers or {})
        assert response.status_code == 200, response.text
        body = response.json()
        ids += [row["id"] for row in body["data"]]
        cursor = body["next_cursor"]
        if cursor is None:
            return ids
    raise AssertionError("paging never terminated")


async def interleaved(engine: AsyncEngine, tag: str) -> tuple[list[UUID], list[UUID]]:
    """Two bookable mentors and two idle ones, created alternately so that newest
    first alone would interleave them. Returns each group newest first."""
    idle_old = await make_public_mentor(engine, f"{tag}-idle-old")
    live_old = await make_bookable_mentor(engine, f"{tag}-live-old")
    idle_new = await make_public_mentor(engine, f"{tag}-idle-new")
    live_new = await make_bookable_mentor(engine, f"{tag}-live-new")
    return [live_new, live_old], [idle_new, idle_old]


async def test_browse_lists_every_bookable_mentor_first_newest_first_inside_each(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    live, idle = await interleaved(db_engine, "browse")

    response = await api_client.get(f"{URL}?limit=10")

    assert response.status_code == 200, response.text
    body = response.json()
    assert [row["id"] for row in body["data"]] == [str(m) for m in live + idle]
    assert [row["taking_bookings"] for row in body["data"]] == [True, True, False, False]
    assert body["total"] == 4


async def test_paging_across_the_boundary_returns_everyone_once_in_order(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The keyset carries the group: a cursor on the id alone would compare the
    first idle mentor's id against the last bookable one's and skip or repeat."""
    live, idle = await interleaved(db_engine, "paging")

    assert await walk(api_client) == [str(m) for m in live + idle]


async def test_a_cursor_minted_before_the_group_was_in_it_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Agreed with the frontend: a pre-change browse cursor is a clean 422, which
    Explore already treats as "start again from page 1"."""
    mentor = await make_bookable_mentor(db_engine, "old-cursor")
    old = base64.urlsafe_b64encode(str(mentor).encode()).decode()

    response = await api_client.get(f"{URL}?limit=1&cursor={old}")

    assert response.status_code == 422, response.text


async def test_search_keeps_bookable_mentors_first(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    live, idle = await interleaved(db_engine, "search")
    for mentor in live + idle:
        await set_headline(db_engine, mentor, "Oxford admissions coach")

    found = await walk(api_client, "&q=oxford")

    assert set(found[:2]) == {str(m) for m in live}
    assert set(found[2:]) == {str(m) for m in idle}


async def test_the_goal_ranking_keeps_bookable_mentors_first(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """An idle mentor covering the goal still ranks below a bookable one who does
    not: being bookable is the first question, goal overlap the second."""
    _, headers = await a_mentee(db_engine, "rank", (INTERVIEW,))
    covering_idle = await make_public_mentor(db_engine, "rank-idle")
    await give_offering(db_engine, covering_idle, INTERVIEW)
    covering_live = await a_mentor(db_engine, "rank-live-covering", INTERVIEW)
    plain_live = await a_mentor(db_engine, "rank-live-plain")

    found = await walk(api_client, headers=headers)

    assert found == [str(covering_live), str(plain_live), str(covering_idle)]


async def test_a_cursor_naming_no_known_group_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "odd-group")
    forged = common.encode_cursor("maybe", mentor)

    response = await api_client.get(f"{URL}?limit=1&cursor={forged}")

    assert response.status_code == 422, response.text
