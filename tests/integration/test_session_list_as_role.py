"""`as=mentor|mentee` narrows a session list to one side (Mentor Dashboard).

A person may be a mentor and a mentee at once, and the list carries both sides
by design. A mentor's dashboard asking for its next session must not get the
mentor's own booking as a mentee, and filtering client-side breaks paging the
way it did for lapsed requests: a page shortened after the limit. So the side is
a filter in the paged statement.
"""

from __future__ import annotations

from uuid import UUID

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import make_bookable_mentor
from tests.integration.test_api_me_mentee_sessions import sign_in
from tests.integration.test_api_me_pending_bookings import CONFIRMED, a_session, a_user

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def listed(
    client: httpx.AsyncClient, user: UUID, headers: dict[str, str], **params: str
) -> httpx.Response:
    return await client.get(f"/api/v1/users/{user}/sessions", params=params, headers=headers)


async def both_sides(db_engine: AsyncEngine, tag: str) -> tuple[UUID, dict[str, str], str, str]:
    """A mentor who also books another mentor: one session on each side, the
    mentee-side one sooner."""
    dual = await make_bookable_mentor(db_engine, f"{tag}-dual")
    other = await make_bookable_mentor(db_engine, f"{tag}-other")
    mentee = await a_user(db_engine)
    as_mentor = await a_session(db_engine, dual, mentee, days_ahead=5, status=CONFIRMED)
    as_mentee = await a_session(db_engine, other, dual, days_ahead=3, status=CONFIRMED)
    return dual, await sign_in(db_engine, dual), str(as_mentor), str(as_mentee)


async def test_as_mentor_lists_only_the_sessions_they_mentor(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    dual, headers, as_mentor, as_mentee = await both_sides(db_engine, "as-mentor")

    mentoring = await listed(api_client, dual, headers, **{"as": "mentor"})
    attending = await listed(api_client, dual, headers, **{"as": "mentee"})
    everything = await listed(api_client, dual, headers)
    for response in (mentoring, attending, everything):
        assert response.status_code == 200, response.text

    assert [r["id"] for r in mentoring.json()["data"]] == [as_mentor]
    assert [r["id"] for r in attending.json()["data"]] == [as_mentee]
    assert {r["id"] for r in everything.json()["data"]} == {as_mentor, as_mentee}


async def test_the_side_counts_toward_the_page_not_after_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`limit=1`, soonest first: the mentee booking is sooner, and must not take
    the page and leave the mentored session behind a cursor."""
    dual, headers, as_mentor, _ = await both_sides(db_engine, "as-page")
    page = await listed(api_client, dual, headers, **{"as": "mentor", "order": "asc", "limit": "1"})
    assert page.status_code == 200, page.text

    assert [r["id"] for r in page.json()["data"]] == [as_mentor]
    assert page.json()["next_cursor"] is None


@pytest.mark.parametrize("side", ["admin", "observer"])
async def test_an_unknown_side_is_a_422(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, side: str
) -> None:
    """**`observer` included**: a real session role, but no session column holds
    it, so it is not a side a list can ask for rather than a quiet alias."""
    dual, headers, _, _ = await both_sides(db_engine, f"as-bad-{side}")

    refused = await listed(api_client, dual, headers, **{"as": side})

    assert refused.status_code == 422, refused.text
