"""Mentor discovery — the endpoint that hands out the ids the others need.

**Three things must be true at once for a mentor to appear**: public, offering
something with a duration, and having declared hours. Each refusal test builds a
mentor who appears and then removes exactly one, because a test that omits two
things proves neither.

Paging is *walked* rather than reasoned about. This is the first list ordered by
id — ADR 0016's base case — and the cursor is `mentor_profiles.id`, which is not
the `id` in the row. A cursor built from the visible id would page through a
different sequence and look almost right.
"""

from __future__ import annotations

from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import (
    add_availability,
    add_session_type,
    make_bookable_mentor,
    make_public_mentor,
)

pytestmark = [pytest.mark.db, pytest.mark.anyio]

URL = "/api/v1/mentors"


async def ids(client: httpx.AsyncClient, url: str = URL) -> list[str]:
    response = await client.get(url)
    assert response.status_code == 200, response.text
    return [row["id"] for row in response.json()["data"]]


async def give_offering(engine: AsyncEngine, mentor: UUID, slug: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO mentor_service_offerings (mentor_user_id, service_offering_id) "
                "SELECT :u, id FROM service_offerings WHERE slug = :s"
            ),
            {"u": mentor, "s": slug},
        )


# --------------------------------------------------------------------------
# Who appears
# --------------------------------------------------------------------------


async def test_a_bookable_mentor_is_listed_without_a_token(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "listed")

    response = await api_client.get(URL)

    assert response.status_code == 200
    body = response.json()
    assert str(mentor) in [row["id"] for row in body["data"]]
    row = next(r for r in body["data"] if r["id"] == str(mentor))
    assert row["first_name"] == "Ada"
    assert row["headline"] == "M"


async def test_the_card_carries_the_offerings_a_mentee_matches_on(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`sort_order`, and batched — one query for the whole page, not one each."""
    mentor = await make_bookable_mentor(db_engine, "offerings")
    await give_offering(db_engine, mentor, "interview-preparation")  # sort_order 60
    await give_offering(db_engine, mentor, "test-preparation")  # sort_order 10

    body = (await api_client.get(URL)).json()
    row = next(r for r in body["data"] if r["id"] == str(mentor))

    assert [o["slug"] for o in row["offerings"]] == [
        "test-preparation",
        "interview-preparation",
    ]


async def test_a_mentor_with_no_user_profile_row_is_still_listed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Outer join. An inner one would make every mentor who never wrote a bio
    unfindable, while their profile page works perfectly."""
    mentor = await make_bookable_mentor(db_engine, "no-profile-row")

    listed = await ids(api_client)

    assert str(mentor) in listed


async def test_a_mentor_with_no_offerings_claimed_is_still_listed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Service offerings are **not** part of `mentor_is_bookable()`.

    A mentor who has claimed none is genuinely bookable — they simply match no
    service filter and show an empty taxonomy. Hiding a working mentor over a
    missing tag would be the wrong trade.
    """
    mentor = await make_bookable_mentor(db_engine, "untagged")

    body = (await api_client.get(URL)).json()
    row = next(r for r in body["data"] if r["id"] == str(mentor))

    assert row["offerings"] == []


# --------------------------------------------------------------------------
# Who does not — one missing thing at a time
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "knob"),
    [("unlisted", {"listed": False}), ("unapproved", {"approved": False})],
)
async def test_a_mentor_who_is_not_public_is_absent(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, reason: str, knob: dict
) -> None:
    mentor = await make_bookable_mentor(db_engine, f"absent-{reason}", **knob)

    assert str(mentor) not in await ids(api_client)


@pytest.mark.parametrize("table", ["mentor_profiles", "users"])
async def test_a_soft_deleted_mentor_is_absent(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, table: str
) -> None:
    """Two soft deletes on two tables, both of which shipped as bugs elsewhere."""
    mentor = await make_bookable_mentor(db_engine, f"absent-deleted-{table.replace('_', '-')}")
    statement = {
        "mentor_profiles": "UPDATE mentor_profiles SET deleted_at = now() WHERE user_id = :u",
        "users": "UPDATE users SET deleted_at = now() WHERE id = :u",
    }[table]
    async with db_engine.begin() as conn:
        await conn.execute(text(statement), {"u": mentor})

    assert str(mentor) not in await ids(api_client)


async def test_a_mentor_with_no_session_type_is_absent(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Nothing to book. `/slots` would 404, and a card linking to a 404 is worse
    than no card."""
    mentor = await make_public_mentor(db_engine, "no-type")
    await add_availability(db_engine, mentor)

    assert str(mentor) not in await ids(api_client)


async def test_a_mentor_whose_offering_has_no_booking_config_is_absent(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A session type without a config has no duration, so it has no slot length.
    The offering exists and still cannot be booked."""
    mentor = await make_public_mentor(db_engine, "no-config")
    await add_session_type(db_engine, mentor, config=False)
    await add_availability(db_engine, mentor)

    assert str(mentor) not in await ids(api_client)


@pytest.mark.parametrize("how", ["none", "inactive", "deleted"])
async def test_a_mentor_with_no_live_hours_is_absent(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, how: str
) -> None:
    """Rules are *recurring weekly*, so having none does not mean "nothing free
    this week" — it means no slot will ever be generated until they act."""
    mentor = await make_public_mentor(db_engine, f"no-hours-{how}")
    await add_session_type(db_engine, mentor)
    if how == "inactive":
        await add_availability(db_engine, mentor, active=False)
    elif how == "deleted":
        await add_availability(db_engine, mentor, deleted=True)

    assert str(mentor) not in await ids(api_client)


@pytest.mark.parametrize("how", ["inactive", "deleted"])
async def test_a_mentor_whose_only_offering_is_gone_is_absent(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, how: str
) -> None:
    mentor = await make_public_mentor(db_engine, f"dead-type-{how}")
    await add_session_type(db_engine, mentor, active=how != "inactive", deleted=how == "deleted")
    await add_availability(db_engine, mentor)

    assert str(mentor) not in await ids(api_client)


# --------------------------------------------------------------------------
# Paging
# --------------------------------------------------------------------------


async def test_paging_returns_every_mentor_exactly_once(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Walked, not reasoned about.

    The cursor is `mentor_profiles.id` and the row's `id` is the **user** — two
    different values. Building the cursor from the visible one would page through
    a different sequence and lose or repeat rows at each boundary, while still
    looking plausible on page one.
    """
    expected = {str(await make_bookable_mentor(db_engine, f"page-{n}")) for n in range(3)}

    seen: list[str] = []
    url: str | None = f"{URL}?limit=1"
    for _ in range(5):  # bounded: a cursor that never advances fails rather than hangs
        assert url is not None
        response = await api_client.get(url)
        assert response.status_code == 200
        body = response.json()
        seen.extend(row["id"] for row in body["data"])
        if body["next_cursor"] is None:
            url = None
            break
        url = f"{URL}?limit=1&cursor={body['next_cursor']}"

    assert url is None, "paging never terminated"
    assert len(seen) == len(set(seen)), "a mentor appeared on two pages"
    assert set(seen) == expected


async def test_a_malformed_cursor_is_a_client_error(api_client: httpx.AsyncClient) -> None:
    assert (await api_client.get(f"{URL}?cursor=not-a-cursor")).status_code == 422


# --------------------------------------------------------------------------
# The chain this endpoint exists to start
# --------------------------------------------------------------------------


async def test_an_id_from_the_list_resolves_at_the_profile(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The whole point: discovery hands out ids the other public reads need.

    Nothing else asserts it, and every part could be individually correct while
    the chain is broken.
    """
    await make_bookable_mentor(db_engine, "chain")

    listed = await ids(api_client)
    assert listed

    profile = await api_client.get(f"{URL}/{listed[0]}")

    assert profile.status_code == 200
    assert profile.json()["id"] == listed[0]
    assert profile.json()["session_types"], "the listed mentor must have something bookable"


async def test_a_deactivated_offering_disappears_from_every_profile(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The `is_active` filter, pinned — raised in review as unguarded.

    `service_offerings.is_active` has no application writer, exactly like
    `users.deleted_at`, and that one was pinned in identical circumstances two
    pull requests ago. The inconsistency was the reason to close this rather than
    the reasoning about whether the state can arise.

    It can: #53 makes the six rows platform-owned, and retiring one is the
    supported use of the column. Without this test the filter is a single
    unreferenced line that reads as dead code to whoever next tidies the file —
    and a retired offering silently reappears on every profile.
    """
    mentor = await make_bookable_mentor(db_engine, "retired")
    await give_offering(db_engine, mentor, "test-preparation")
    await give_offering(db_engine, mentor, "interview-preparation")

    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE service_offerings SET is_active = false WHERE slug = 'test-preparation'")
        )

    card = next(r for r in (await api_client.get(URL)).json()["data"] if r["id"] == str(mentor))
    profile = (await api_client.get(f"{URL}/{mentor}")).json()

    assert [o["slug"] for o in card["offerings"]] == ["interview-preparation"]
    assert [o["slug"] for o in profile["offerings"]] == ["interview-preparation"]


async def test_a_mentor_with_several_offerings_and_windows_appears_once(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`EXISTS`, not joins — asserted rather than reasoned about.

    Two session types and two availability rules would multiply to four rows if
    either predicate were written as a join. On a keyset-paged list that does not
    merely repeat a card: the duplicates consume the page limit and the cursor
    advances past mentors nobody saw, so rows are lost at the boundary rather
    than shown twice.
    """
    mentor = await make_public_mentor(db_engine, "several")
    await add_session_type(db_engine, mentor, name="Quick chat", duration=15)
    await add_session_type(db_engine, mentor, name="Deep dive", duration=60)
    await add_availability(db_engine, mentor, day_of_week=1)
    await add_availability(db_engine, mentor, day_of_week=2)

    listed = await ids(api_client)

    assert listed.count(str(mentor)) == 1


async def test_each_card_carries_its_own_offerings(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Two mentors, different offerings — because one mentor cannot test a
    per-mentor grouping.

    The batched query fetches the whole page in one statement and groups by
    mentor afterwards. With a single mentor in the fixture, attributing every row
    to the first id is indistinguishable from grouping correctly, and a mutation
    doing exactly that survived. This is the same shape as the owner-scope gap
    found two pull requests ago: **a fixture holding one of something cannot test
    a filter on it.**
    """
    first = await make_bookable_mentor(db_engine, "cards-first")
    second = await make_bookable_mentor(db_engine, "cards-second")
    await give_offering(db_engine, first, "test-preparation")
    await give_offering(db_engine, second, "interview-preparation")

    body = (await api_client.get(URL)).json()
    cards = {row["id"]: [o["slug"] for o in row["offerings"]] for row in body["data"]}

    assert cards[str(first)] == ["test-preparation"]
    assert cards[str(second)] == ["interview-preparation"]


async def test_paging_follows_when_a_mentor_became_one_after_someone_else(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The order is *became a mentor*, not *signed up* — and they differ.

    Every fixture elsewhere creates the user and the mentor profile microseconds
    apart, so `users.id` and `mentor_profiles.id` sort identically and a cursor
    built from the wrong one still works. A mutation doing exactly that survived
    the paging test.

    Here three users exist first and their profiles are created in the reverse
    order — a mentee of some standing who starts mentoring after a newer signup,
    which is the case the choice of sort key was justified by. Now the two
    orderings disagree, and a cursor on the visible id skips and repeats.
    """
    async with db_engine.begin() as conn:
        users = [
            (
                await conn.execute(
                    text(
                        "INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
                        "VALUES (:e, gen_random_uuid(), 'Ada', 'mentor', 'UTC') RETURNING id"
                    ),
                    {"e": f"late-{n}@example.test"},
                )
            ).scalar_one()
            for n in range(3)
        ]
        # The profiles are created in the **opposite** order, so `users.id` and
        # `mentor_profiles.id` disagree. Everywhere else both are inserted in one
        # breath and sort identically, which is why a cursor on the wrong column
        # went unnoticed.
        for user_id in reversed(users):
            await conn.execute(
                text(
                    "INSERT INTO mentor_profiles "
                    "(user_id, headline, approval_status, listing_status) "
                    "VALUES (:u, 'M', 'approved', 'listed')"
                ),
                {"u": user_id},
            )
    for user_id in users:
        await add_session_type(db_engine, user_id)
        await add_availability(db_engine, user_id)

    seen: list[str] = []
    url: str | None = f"{URL}?limit=1"
    for _ in range(5):
        assert url is not None
        body = (await api_client.get(url)).json()
        seen.extend(row["id"] for row in body["data"])
        if body["next_cursor"] is None:
            url = None
            break
        url = f"{URL}?limit=1&cursor={body['next_cursor']}"

    assert url is None, "paging never terminated"
    assert len(seen) == len(set(seen)), "a mentor appeared on two pages"
    assert set(seen) == {str(u) for u in users}
