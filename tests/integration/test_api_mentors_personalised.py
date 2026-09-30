"""`/mentors` for a signed-in viewer: ranked by their goals, never listing themself.

The agreed order (Explore reply, item 8):

* logged out — newest first, unchanged;
* signed in **with goals** — mentors giving more of the mentee's goal offerings
  first; ties shuffled, but the shuffle is fixed per user per day so paging is
  stable;
* with `q` — best match first, unchanged.

A signed-in mentor never sees themself, and `total` agrees with the pages.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import make_bookable_mentor
from tests.integration.test_api_mentors import give_offering

from conftest import PLATFORM_WINDOW, api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]

URL = "/api/v1/mentors"
INTERVIEW = "interview-preparation"
TESTS = "test-preparation"
SCHOLARSHIPS = "scholarships-financial-aid"


async def a_mentee(
    engine: AsyncEngine, tag: str, goals: tuple[str, ...] = ()
) -> tuple[UUID, dict[str, str]]:
    """A signed-in mentee, with each slug in `goals` as a goal need."""
    auth_id = uuid4()
    async with engine.begin() as conn:
        user = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, auth_id, primary_role, timezone) "
                    "VALUES (:e, :a, 'mentee', 'UTC') RETURNING id"
                ),
                {"e": f"mentee-{tag}@example.com", "a": auth_id},
            )
        ).scalar_one()
        if goals:
            await conn.execute(text("INSERT INTO mentee_goals (user_id) VALUES (:u)"), {"u": user})
            for slug in goals:
                await conn.execute(
                    text(
                        "INSERT INTO mentee_goal_needs (user_id, service_offering_id) "
                        "SELECT :u, id FROM service_offerings WHERE slug = :s"
                    ),
                    {"u": user, "s": slug},
                )
    return user, bearer(api_token(auth_id))


async def a_mentor(engine: AsyncEngine, tag: str, *offerings: str) -> UUID:
    mentor = await make_bookable_mentor(engine, tag)
    for slug in offerings:
        await give_offering(engine, mentor, slug)
    return mentor


async def listed(
    client: httpx.AsyncClient, headers: dict[str, str] | None = None, query: str = ""
) -> list[str]:
    """Every mentor id across every page, in order."""
    ids: list[str] = []
    cursor: str | None = None
    while True:
        page_query = f"?limit=2{query}" + (f"&cursor={cursor}" if cursor else "")
        body = (await client.get(URL + page_query, headers=headers or {})).json()
        ids += [row["id"] for row in body["data"]]
        cursor = body["next_cursor"]
        if cursor is None:
            return ids


async def test_mentors_covering_more_goals_come_first(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    both = await a_mentor(db_engine, "rank-both", INTERVIEW, TESTS)
    one = await a_mentor(db_engine, "rank-one", INTERVIEW)
    # Newest of all, so newest-first would put it at the top.
    none = await a_mentor(db_engine, "rank-none", SCHOLARSHIPS)
    _, headers = await a_mentee(db_engine, "rank", goals=(INTERVIEW, TESTS))

    order = await listed(api_client, headers)

    assert [m for m in order if m in {str(both), str(one), str(none)}] == [
        str(both),
        str(one),
        str(none),
    ]


async def test_paging_the_ranked_list_shows_everyone_once(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Ties are shuffled, and the shuffle must hold still between pages — or a
    mentor is shown twice while another never appears."""
    mentors = {str(await a_mentor(db_engine, f"page-{n}", INTERVIEW)) for n in range(5)}
    _, headers = await a_mentee(db_engine, "page", goals=(INTERVIEW,))

    order = await listed(api_client, headers)

    assert len(order) == len(set(order))
    assert mentors <= set(order)


async def test_the_shuffle_is_the_same_on_every_request(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    for n in range(5):
        await a_mentor(db_engine, f"stable-{n}", TESTS)
    _, headers = await a_mentee(db_engine, "stable", goals=(TESTS,))

    assert await listed(api_client, headers) == await listed(api_client, headers)


async def test_different_mentees_see_ties_in_different_orders(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Six mentors tied on one goal: the chance that four mentees all get the
    same order by accident is (1/720)^3."""
    tied = {str(await a_mentor(db_engine, f"tie-{n}", SCHOLARSHIPS)) for n in range(6)}
    orders = set()
    for n in range(4):
        _, headers = await a_mentee(db_engine, f"tie-viewer-{n}", goals=(SCHOLARSHIPS,))
        orders.add(tuple(m for m in await listed(api_client, headers) if m in tied))

    assert len(orders) > 1


async def test_a_mentee_without_goals_sees_newest_first(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    older = await a_mentor(db_engine, "plain-older", INTERVIEW)
    newer = await a_mentor(db_engine, "plain-newer", TESTS)
    _, headers = await a_mentee(db_engine, "plain")

    order = await listed(api_client, headers)

    assert order.index(str(newer)) < order.index(str(older))
    assert order == await listed(api_client)


async def test_a_search_is_ranked_by_the_search_not_the_goals(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    await a_mentor(db_engine, "search-a", INTERVIEW)
    await a_mentor(db_engine, "search-b", TESTS)
    _, headers = await a_mentee(db_engine, "search", goals=(TESTS,))

    assert await listed(api_client, headers, "&q=Lovelace") == await listed(
        api_client, query="&q=Lovelace"
    )


async def test_the_offering_filter_still_applies_to_the_ranked_list(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    kept = await a_mentor(db_engine, "filter-kept", INTERVIEW)
    dropped = await a_mentor(db_engine, "filter-dropped", TESTS)
    _, headers = await a_mentee(db_engine, "filter", goals=(INTERVIEW, TESTS))

    order = await listed(api_client, headers, f"&offering={INTERVIEW}")

    assert str(kept) in order
    assert str(dropped) not in order


async def test_a_signed_in_mentor_never_sees_themself(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await a_mentor(db_engine, "self", INTERVIEW)
    async with db_engine.begin() as conn:
        auth_id = (
            await conn.execute(text("SELECT auth_id FROM users WHERE id = :u"), {"u": mentor})
        ).scalar_one()
        await conn.execute(
            text("UPDATE users SET email = 'self-mentor@example.com' WHERE id = :u"),
            {"u": mentor},
        )
    headers = bearer(api_token(auth_id))

    anonymous = (await api_client.get(URL)).json()
    signed_in = (await api_client.get(URL, headers=headers)).json()

    assert str(mentor) in await listed(api_client)
    assert str(mentor) not in await listed(api_client, headers)
    assert signed_in["total"] == anonymous["total"] - 1


async def test_the_list_is_marked_per_viewer(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, headers = await a_mentee(db_engine, "headers")

    anonymous = await api_client.get(URL)
    signed_in = await api_client.get(URL, headers=headers)

    assert "authorization" in anonymous.headers["vary"].lower()
    assert "private" not in anonymous.headers.get("cache-control", "")
    assert "private" in signed_in.headers["cache-control"]


async def test_a_bad_token_gets_the_public_list(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    await a_mentor(db_engine, "bad-token", INTERVIEW)

    response = await api_client.get(URL, headers={"Authorization": "Bearer not-a-jwt"})

    assert response.status_code == 200
    assert [row["id"] for row in response.json()["data"]] == [
        row["id"] for row in (await api_client.get(URL)).json()["data"]
    ]


async def test_the_shuffle_changes_with_the_day(db_engine: AsyncEngine) -> None:
    """Fixed per user *per day*: asked directly, since the route reads today."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.infra.db.mentor_search_store import search_mentors

    tied = [await a_mentor(db_engine, f"day-{n}", INTERVIEW) for n in range(6)]
    viewer, _ = await a_mentee(db_engine, "day", goals=(INTERVIEW,))
    orders = set()
    async with AsyncSession(db_engine) as session:
        for day in range(4):
            rows, _ = await search_mentors(
                session,
                window=PLATFORM_WINDOW,
                limit=50,
                viewer=viewer,
                goal_day=dt.date(2030, 1, 1) + dt.timedelta(days=day),
            )
            orders.add(tuple(row["user_id"] for row in rows if row["user_id"] in tied))

    assert len(orders) > 1


async def test_a_token_that_lapses_mid_paging_keeps_paging(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Page one goal-ranked, page two anonymous (the tab resumed after the
    token's hour): the offset cursor must keep working, not 422."""
    for n in range(3):
        await a_mentor(db_engine, f"lapse-{n}", INTERVIEW)
    _, headers = await a_mentee(db_engine, "lapse", goals=(INTERVIEW,))

    first = (await api_client.get(f"{URL}?limit=1", headers=headers)).json()
    second = await api_client.get(f"{URL}?limit=1&cursor={first['next_cursor']}")

    assert second.status_code == 200
    # It continues from the cursor's position, newest first, rather than
    # starting the list again.
    newest_first = await listed(api_client)
    assert [row["id"] for row in second.json()["data"]] == newest_first[1:2]


async def test_signing_in_mid_paging_keeps_paging(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The other way: an anonymous id cursor, then a mentee with goals."""
    for n in range(3):
        await a_mentor(db_engine, f"signin-{n}", INTERVIEW)
    _, headers = await a_mentee(db_engine, "signin", goals=(INTERVIEW,))

    first = (await api_client.get(f"{URL}?limit=1")).json()
    second = await api_client.get(f"{URL}?limit=1&cursor={first['next_cursor']}", headers=headers)

    assert second.status_code == 200
    assert second.json()["data"]


async def test_a_retired_goal_offering_lifts_nobody(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A retired offering is off every card; it must not rank a mentor either.
    With it the mentee's only goal, the list falls back to newest first."""
    older = await a_mentor(db_engine, "retired-older", SCHOLARSHIPS)
    newer = await a_mentor(db_engine, "retired-newer", TESTS)
    _, headers = await a_mentee(db_engine, "retired", goals=(SCHOLARSHIPS,))
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE service_offerings SET is_active = false WHERE slug = :s"),
            {"s": SCHOLARSHIPS},
        )

    order = await listed(api_client, headers)

    assert order.index(str(newer)) < order.index(str(older))
