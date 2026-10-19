"""The schools a mentee's goal names (Mentee Settings PRD).

`PUT /users/{id}/goal` takes `schools`: omitted leaves them, `[]` clears them,
anything else replaces the whole list in the order sent. `GET` returns them by
position. A typed school goes through the education entry's own resolution
(`education_writer.resolve_institution`): a supplied id is validated, a name
is matched exactly or case-folded, and anything else becomes a `pending_review`
institution the caller created.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_admin import ADMIN, add_institution
from tests.integration.test_api_admin import make_user as make_admin
from tests.integration.test_api_writes import make_user, url

from app.domain.goals import MAX_GOAL_SCHOOLS, SCHOOL_NAME_MAX_LENGTH
from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def _owner(engine: AsyncEngine, email: str) -> tuple[UUID, dict[str, str]]:
    auth_id = uuid4()
    user = await make_user(engine, auth_id, email)
    return user, bearer(api_token(auth_id))


async def _put(
    client: httpx.AsyncClient, user: UUID, headers: dict[str, str], body: dict[str, Any]
) -> httpx.Response:
    return await client.put(url(user, "goal"), headers=headers, json=body)


async def _schools(
    client: httpx.AsyncClient, user: UUID, headers: dict[str, str]
) -> list[dict[str, Any]]:
    response = await client.get(url(user, "goal"), headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["schools"]  # type: ignore[no-any-return]


async def test_schools_save_in_order_and_resolve_like_education(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user, headers = await _owner(db_engine, "schools@example.com")
    picked = await add_institution(db_engine, "Picked University", status="approved")
    await add_institution(db_engine, "Matched College", status="approved")

    saved = await _put(
        api_client,
        user,
        headers,
        {
            "schools": [
                {"institution_id": str(picked), "school_name_raw": "Picked University"},
                {"school_name_raw": "matched college"},
                {"school_name_raw": "Nowhere Listed Institute"},
            ]
        },
    )
    assert saved.status_code == 200, saved.text

    schools = await _schools(api_client, user, headers)
    assert [s["position"] for s in schools] == [0, 1, 2]
    assert [s["school_name_raw"] for s in schools] == [
        "Picked University",
        "matched college",
        "Nowhere Listed Institute",
    ]
    assert schools[0]["institution_id"] == str(picked)
    assert schools[0]["institution_name"] == "Picked University"
    # Case-folded match, the education entry's rule: linked, typed text kept.
    assert schools[1]["institution_name"] == "Matched College"
    # Unmatched: a pending institution the caller created, as education does.
    assert schools[2]["institution_name"] == "Nowhere Listed Institute"
    async with db_engine.connect() as conn:
        created = (
            await conn.execute(
                text("SELECT status, created_by FROM institutions WHERE id = :i"),
                {"i": schools[2]["institution_id"]},
            )
        ).one()
    assert (str(created.status), created.created_by) == ("pending_review", user)
    assert len({s["id"] for s in schools}) == 3


async def test_a_school_id_the_caller_may_not_pick_is_saved_unlinked(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Somebody else's pending row is not selectable, exactly as on education:
    the typed name is kept and the link is left empty."""
    user, headers = await _owner(db_engine, "unlinked@example.com")
    theirs = await add_institution(db_engine, "Their Pending School")

    saved = await _put(
        api_client,
        user,
        headers,
        {"schools": [{"institution_id": str(theirs), "school_name_raw": "Their Pending School"}]},
    )
    assert saved.status_code == 200, saved.text

    [school] = await _schools(api_client, user, headers)
    assert school["institution_id"] is None
    assert school["institution_name"] is None
    assert school["school_name_raw"] == "Their Pending School"


async def test_omitted_leaves_an_empty_list_clears_and_a_list_replaces(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user, headers = await _owner(db_engine, "replace@example.com")
    first = [{"school_name_raw": "Alpha"}, {"school_name_raw": "Beta"}]
    assert (await _put(api_client, user, headers, {"schools": first})).status_code == 200
    before = await _schools(api_client, user, headers)

    assert (await _put(api_client, user, headers, {"notes": "only notes"})).status_code == 200
    assert await _schools(api_client, user, headers) == before

    assert (await _put(api_client, user, headers, {"schools": None})).status_code == 200
    assert await _schools(api_client, user, headers) == before

    replaced = await _put(api_client, user, headers, {"schools": [{"school_name_raw": "Gamma"}]})
    assert replaced.status_code == 200
    after = await _schools(api_client, user, headers)
    assert [(s["school_name_raw"], s["position"]) for s in after] == [("Gamma", 0)]
    assert after[0]["id"] not in {s["id"] for s in before}

    assert (await _put(api_client, user, headers, {"schools": []})).status_code == 200
    assert await _schools(api_client, user, headers) == []


async def test_ten_schools_save_and_an_eleventh_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user, headers = await _owner(db_engine, "cap@example.com")
    ten = [{"school_name_raw": f"School {n}"} for n in range(MAX_GOAL_SCHOOLS)]
    assert (await _put(api_client, user, headers, {"schools": ten})).status_code == 200

    eleven = [*ten, {"school_name_raw": "One Too Many"}]
    refused = await _put(api_client, user, headers, {"schools": eleven})

    assert refused.status_code == 422, refused.text
    assert [e["pointer"] for e in refused.json()["errors"]] == ["/schools"]
    assert len(await _schools(api_client, user, headers)) == MAX_GOAL_SCHOOLS


@pytest.mark.parametrize(
    "name", ["x" * (SCHOOL_NAME_MAX_LENGTH + 1), "   "], ids=["one-over", "blank"]
)
async def test_a_school_name_outside_the_bound_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, name: str
) -> None:
    user, headers = await _owner(db_engine, "long@example.com")

    refused = await _put(
        api_client,
        user,
        headers,
        {"schools": [{"school_name_raw": "Fine"}, {"school_name_raw": name}]},
    )

    assert refused.status_code == 422, refused.text
    assert [e["pointer"] for e in refused.json()["errors"]] == ["/schools/1/school_name_raw"]


async def test_the_longest_school_name_is_accepted(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user, headers = await _owner(db_engine, "longest@example.com")
    name = "x" * SCHOOL_NAME_MAX_LENGTH

    saved = await _put(api_client, user, headers, {"schools": [{"school_name_raw": name}]})

    assert saved.status_code == 200, saved.text
    assert [s["school_name_raw"] for s in await _schools(api_client, user, headers)] == [name]


async def test_a_user_reads_and_replaces_only_their_own_schools(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Scoped in the query on both paths (non-negotiable 5): a second user's
    schools neither appear on this goal nor change when this one is replaced."""
    mine, my_headers = await _owner(db_engine, "mine@example.com")
    theirs, their_headers = await _owner(db_engine, "theirs@example.com")
    await _put(api_client, theirs, their_headers, {"schools": [{"school_name_raw": "Theirs"}]})
    await _put(api_client, mine, my_headers, {"schools": [{"school_name_raw": "Mine"}]})
    await _put(api_client, mine, my_headers, {"schools": []})

    assert await _schools(api_client, mine, my_headers) == []
    assert [s["school_name_raw"] for s in await _schools(api_client, theirs, their_headers)] == [
        "Theirs"
    ]
    refused = await api_client.get(url(theirs, "goal"), headers=my_headers)
    assert refused.status_code == 404


async def test_me_carries_the_same_schools(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user, headers = await _owner(db_engine, "me-schools@example.com")
    await _put(api_client, user, headers, {"schools": [{"school_name_raw": "On Me"}]})

    me = (await api_client.get("/api/v1/me", headers=headers)).json()

    assert me["goal"]["schools"] == await _schools(api_client, user, headers)


async def test_clearing_the_goal_takes_its_schools_countries_and_needs(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """What the route description says: all three reference the goal and
    cascade. Another user's are untouched."""
    user, headers = await _owner(db_engine, "cleared@example.com")
    other, other_headers = await _owner(db_engine, "kept@example.com")
    async with db_engine.connect() as conn:
        ng = (await conn.execute(text("SELECT id FROM countries WHERE code = 'NG'"))).scalar_one()
        need = (
            await conn.execute(
                text("SELECT id FROM service_offerings WHERE slug = 'school-selection'")
            )
        ).scalar_one()
    body = {
        "schools": [{"school_name_raw": "Gone Soon"}],
        "country_ids": [str(ng)],
        "need_ids": [str(need)],
    }
    assert (await _put(api_client, user, headers, body)).status_code == 200
    assert (await _put(api_client, other, other_headers, body)).status_code == 200

    removed = await api_client.delete(url(user, "goal"), headers=headers)

    assert removed.status_code == 204
    async with db_engine.connect() as conn:
        left = (
            await conn.execute(
                text(
                    "SELECT (SELECT count(*) FROM mentee_goal_schools), "
                    "(SELECT count(*) FROM mentee_goal_countries), "
                    "(SELECT count(*) FROM mentee_goal_needs)"
                )
            )
        ).one()
    assert tuple(left) == (1, 1, 1)


async def test_a_merge_repoints_goal_schools_and_the_queue_counts_them(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Decision 65: a merge repoints every reference, so a goal school never
    points at a retired row; and the queue's `uses` counts the people waiting
    on a school, from a goal as well as from education."""
    admin_auth = uuid4()
    await make_admin(db_engine, admin_auth, "admin@example.com", role="super_admin")
    admin = bearer(api_token(admin_auth))
    winner = await add_institution(db_engine, "University of Ibadan", status="approved")
    user, headers = await _owner(db_engine, "goal-merge@example.com")
    await _put(api_client, user, headers, {"schools": [{"school_name_raw": "Univ of Ibadn"}]})
    [school] = await _schools(api_client, user, headers)
    loser = school["institution_id"]

    queue = (await api_client.get(f"{ADMIN}/institutions/pending", headers=admin)).json()
    assert [(row["name"], row["uses"]) for row in queue["data"]] == [("Univ of Ibadn", 1)]

    merged = await api_client.post(
        f"{ADMIN}/institutions/{loser}/merge", json={"winning_id": str(winner)}, headers=admin
    )

    assert merged.status_code == 200, merged.text
    [after] = await _schools(api_client, user, headers)
    assert after["institution_id"] == str(winner)
    assert after["institution_name"] == "University of Ibadan"
    assert after["school_name_raw"] == "Univ of Ibadn"
