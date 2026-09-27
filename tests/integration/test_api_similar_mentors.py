"""`GET /mentors/{handle}/similar` — up to three mentors who give the same help.

Similar means **shares a service offering**: the closed six-row taxonomy is the
axis matching already runs on. Candidates are exactly who discovery lists —
public and bookable — so a "similar" card never links to a profile a stranger
cannot open or a mentor nobody can book.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import (
    add_completed_sessions,
    make_bookable_mentor,
    make_public_mentor,
)
from tests.integration.test_api_mentor_search import give_offering

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]

TEST_PREP = "test-preparation"  # sort_order 10
DOCS = "document-preparation"  # 20
SCHOOLS = "school-selection"  # 30
INTERVIEWS = "interview-preparation"  # 60


def url(handle: object) -> str:
    return f"/api/v1/mentors/{handle}/similar"


async def mentor_with(engine: AsyncEngine, tag: str, *slugs: str, bookable: bool = True) -> UUID:
    make = make_bookable_mentor if bookable else make_public_mentor
    mentor = await make(engine, tag)
    for slug in slugs:
        await give_offering(engine, mentor, slug)
    return mentor


def ids(body: dict[str, object]) -> list[str]:
    return [card["id"] for card in body["data"]]  # type: ignore[attr-defined]


async def test_mentors_sharing_an_offering_are_similar(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    me = await mentor_with(db_engine, "me", TEST_PREP)
    alike = await mentor_with(db_engine, "alike", TEST_PREP)
    await mentor_with(db_engine, "unlike", INTERVIEWS)

    response = await api_client.get(url(me))

    assert response.status_code == 200
    body = response.json()
    assert ids(body) == [str(alike)]
    card = body["data"][0]
    assert card["shared_offering"] == {"slug": TEST_PREP, "display_name": "Test Preparation"}
    # A full discovery card, not a stub.
    assert {"first_name", "offerings", "next_available_state", "review_count"} <= set(card)
    assert body["next_cursor"] is None


async def test_the_mentor_is_never_similar_to_themself(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    me = await mentor_with(db_engine, "self", TEST_PREP)

    body = (await api_client.get(url(me))).json()

    assert body["data"] == []


async def test_only_bookable_public_mentors_are_suggested(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Discovery's own predicates: a suggestion that 404s, or that nobody can
    book, is worse than no suggestion."""
    me = await mentor_with(db_engine, "gate-me", TEST_PREP)
    shown = await mentor_with(db_engine, "gate-shown", TEST_PREP)
    await mentor_with(db_engine, "gate-unbookable", TEST_PREP, bookable=False)
    hidden = await mentor_with(db_engine, "gate-hidden", TEST_PREP)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE mentor_profiles SET listing_status = 'unlisted' WHERE user_id = :u"),
            {"u": hidden},
        )

    body = (await api_client.get(url(me))).json()

    assert ids(body) == [str(shown)]


async def test_more_shared_offerings_rank_first(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    me = await mentor_with(db_engine, "rank-me", TEST_PREP, DOCS, SCHOOLS)
    one = await mentor_with(db_engine, "rank-one", TEST_PREP)
    three = await mentor_with(db_engine, "rank-three", TEST_PREP, DOCS, SCHOOLS)
    two = await mentor_with(db_engine, "rank-two", DOCS, SCHOOLS)

    body = (await api_client.get(url(me))).json()

    assert ids(body) == [str(three), str(two), str(one)]


async def test_a_tie_goes_to_the_mentor_with_more_delivered_sessions(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    me = await mentor_with(db_engine, "tie-me", TEST_PREP)
    # Proven first, so the final newest-first tie-break would favour `quiet`:
    # only the delivered-sessions rule can put `proven` ahead.
    proven = await mentor_with(db_engine, "tie-proven", TEST_PREP)
    quiet = await mentor_with(db_engine, "tie-quiet", TEST_PREP)
    await add_completed_sessions(db_engine, proven, 3)

    body = (await api_client.get(url(me))).json()

    assert ids(body) == [str(proven), str(quiet)]


async def test_at_most_three_are_suggested(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    me = await mentor_with(db_engine, "cap-me", TEST_PREP)
    for n in range(4):
        await mentor_with(db_engine, f"cap-{n}", TEST_PREP)

    body = (await api_client.get(url(me))).json()

    assert len(body["data"]) == 3


async def test_the_shared_offering_is_the_first_in_platform_order(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Both share interviews and documents; documents sorts first on the
    platform's own order, so that is the reason shown."""
    me = await mentor_with(db_engine, "order-me", INTERVIEWS, DOCS)
    await mentor_with(db_engine, "order-other", INTERVIEWS, DOCS, TEST_PREP)

    card = (await api_client.get(url(me))).json()["data"][0]

    assert card["shared_offering"]["slug"] == DOCS


async def test_a_mentor_with_no_offerings_has_no_similar_mentors(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    me = await mentor_with(db_engine, "bare-me")
    await mentor_with(db_engine, "bare-other", TEST_PREP)

    response = await api_client.get(url(me))

    assert response.status_code == 200
    assert response.json()["data"] == []


async def test_a_hidden_mentor_is_a_404(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    me = await make_public_mentor(db_engine, "hidden-me", approved=False, slug="hidden-me")

    for handle in (me, "hidden-me", uuid4()):
        assert (await api_client.get(url(handle))).status_code == 404


async def test_the_owner_of_a_hidden_profile_gets_no_suggestions_either(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """No owner view here: similar mentors are for a mentee choosing, and a
    hidden profile has no visitors to suggest anything to."""
    me = await make_public_mentor(db_engine, "hidden-owner", approved=False)
    async with db_engine.begin() as conn:
        auth_id = (
            await conn.execute(text("SELECT auth_id FROM users WHERE id = :u"), {"u": me})
        ).scalar_one()

    response = await api_client.get(url(me), headers=bearer(api_token(auth_id)))

    assert response.status_code == 404


async def test_the_list_is_shared_cacheable_unless_a_token_came(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    me = await mentor_with(db_engine, "cache-me", TEST_PREP)

    anonymous = await api_client.get(url(me))
    signed_in = await api_client.get(url(me), headers=bearer(api_token(uuid4())))

    assert anonymous.headers["cache-control"] == "public, max-age=60"
    assert "authorization" in anonymous.headers["vary"].lower()
    assert signed_in.headers["cache-control"] == "private"
