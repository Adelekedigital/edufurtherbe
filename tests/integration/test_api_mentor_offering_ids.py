"""A mentor's `offering_ids`: an unknown, retired or repeated id is a `422`, not a 500.

Before this, the list went straight to `mentor_service_offerings`: an id naming
no offering failed the foreign key and a repeat failed the unique pair, both as
a 500. Now both are refused at a pointer before anything is written, on the
create and the edit alike, through the goal's own checks (`refuse_repeats`,
`_refuse_unknown_offering_ids`).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.application_factories import make_applicant
from tests.integration.factories import make_public_mentor, token_for
from tests.integration.test_api_writes import make_user, url

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def _offerings(engine: AsyncEngine, count: int) -> list[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT id FROM service_offerings WHERE is_active ORDER BY id LIMIT :n"),
            {"n": count},
        )
    return [str(r.id) for r in rows]


async def _held(engine: AsyncEngine, mentor: UUID) -> list[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT service_offering_id FROM mentor_service_offerings "
                "WHERE mentor_user_id = :u ORDER BY service_offering_id"
            ),
            {"u": mentor},
        )
    return [str(r[0]) for r in rows]


async def _retired_offering(engine: AsyncEngine) -> str:
    """A retired offering of this test's own: flipping a shared catalogue row
    would let a concurrent test see it retired."""
    async with engine.begin() as conn:
        row = await conn.execute(
            text(
                "INSERT INTO service_offerings (slug, display_name, is_active) "
                "VALUES (:s, 'Retired', false) RETURNING id"
            ),
            {"s": f"retired-{uuid4().hex[:8]}"},
        )
    return str(row.scalar_one())


async def _headline(engine: AsyncEngine, mentor: UUID) -> object:
    async with engine.connect() as conn:
        row = await conn.execute(
            text("SELECT headline FROM mentor_profiles WHERE user_id = :u"), {"u": mentor}
        )
    return row.scalar_one()


def _pointers(response: httpx.Response) -> list[str]:
    return [e["pointer"] for e in response.json()["errors"]]


async def _patch(
    client: httpx.AsyncClient, engine: AsyncEngine, mentor: UUID, body: dict[str, Any]
) -> httpx.Response:
    headers = bearer(await token_for(engine, mentor))
    return await client.patch(url(mentor, "mentor-profile"), headers=headers, json=body)


async def test_known_offerings_save(api_client: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    mentor = await make_public_mentor(db_engine, "off-ok")
    a, b = await _offerings(db_engine, 2)

    response = await _patch(api_client, db_engine, mentor, {"offering_ids": [a, b]})

    assert response.status_code == 200, response.text
    assert await _held(db_engine, mentor) == sorted([a, b])


async def test_an_unknown_offering_is_a_422_at_its_index_and_writes_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "off-unknown")
    (a,) = await _offerings(db_engine, 1)
    before = await _held(db_engine, mentor)
    headline = await _headline(db_engine, mentor)

    response = await _patch(
        api_client, db_engine, mentor, {"offering_ids": [a, str(uuid4())], "headline": "No"}
    )

    assert response.status_code == 422, response.text
    assert _pointers(response) == ["/offering_ids/1"]
    assert await _held(db_engine, mentor) == before
    assert await _headline(db_engine, mentor) == headline


async def test_a_retired_offering_is_refused_like_an_unknown_one(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "off-retired")
    (a,) = await _offerings(db_engine, 1)
    retired = await _retired_offering(db_engine)

    response = await _patch(api_client, db_engine, mentor, {"offering_ids": [a, retired]})

    assert response.status_code == 422, response.text
    assert _pointers(response) == ["/offering_ids/1"]


async def test_bad_offerings_and_countries_are_named_in_one_422(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "off-both")

    response = await _patch(
        api_client,
        db_engine,
        mentor,
        {"offering_ids": [str(uuid4())], "guide_country_ids": [str(uuid4())]},
    )

    assert response.status_code == 422, response.text
    assert sorted(_pointers(response)) == ["/guide_country_ids/0", "/offering_ids/0"]


async def test_a_mentor_in_review_holding_a_retired_offering_can_resend_their_list(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The read hides a retired offering, so the freeze compares the live ones:
    resending what the mentor can see is not a change (decision 273)."""
    mentor = await make_applicant(db_engine, "off-review")
    async with db_engine.begin() as conn:
        held = (
            (
                await conn.execute(
                    text(
                        "SELECT service_offering_id FROM mentor_service_offerings "
                        "WHERE mentor_user_id = :u"
                    ),
                    {"u": mentor},
                )
            )
            .scalars()
            .all()
        )
        retired = await conn.execute(
            text(
                "INSERT INTO service_offerings (slug, display_name, is_active) "
                "VALUES (:s, 'Retired', false) RETURNING id"
            ),
            {"s": f"retired-{uuid4().hex[:8]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO mentor_service_offerings (mentor_user_id, service_offering_id) "
                "VALUES (:u, :o)"
            ),
            {"u": mentor, "o": retired.scalar_one()},
        )
        await conn.execute(
            text("UPDATE mentor_profiles SET submitted_at = now() WHERE user_id = :u"),
            {"u": mentor},
        )

    response = await _patch(
        api_client,
        db_engine,
        mentor,
        {"offering_ids": [str(o) for o in held], "years_of_experience": 3},
    )

    assert response.status_code == 200, response.text


async def test_an_offering_twice_is_a_422_at_the_list(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "off-twice")
    (a,) = await _offerings(db_engine, 1)

    response = await _patch(api_client, db_engine, mentor, {"offering_ids": [a, a]})

    assert response.status_code == 422, response.text
    assert _pointers(response) == ["/offering_ids"]


async def test_an_approved_mentor_clearing_every_help_area_is_a_422_and_writes_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """An approved mentor always has a help area (owner, 2026-10-25; decision 292)."""
    mentor = await make_public_mentor(db_engine, "off-clear")
    a, b = await _offerings(db_engine, 2)
    assert (await _patch(api_client, db_engine, mentor, {"offering_ids": [a]})).status_code == 200
    headline = await _headline(db_engine, mentor)

    response = await _patch(
        api_client, db_engine, mentor, {"offering_ids": [], "headline": "Changed"}
    )

    assert response.status_code == 422, response.text
    assert _pointers(response) == ["/offering_ids"]
    assert await _held(db_engine, mentor) == [a]
    assert await _headline(db_engine, mentor) == headline
    replaced = await _patch(api_client, db_engine, mentor, {"offering_ids": [b]})
    assert replaced.status_code == 200, replaced.text
    assert await _held(db_engine, mentor) == [b]


async def test_an_unlisted_approved_mentor_cannot_clear_them_either(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Approved, listed or not: pausing does not make a mentor a draft."""
    mentor = await make_public_mentor(db_engine, "off-clear-unlisted", listed=False)
    (a,) = await _offerings(db_engine, 1)
    assert (await _patch(api_client, db_engine, mentor, {"offering_ids": [a]})).status_code == 200

    response = await _patch(api_client, db_engine, mentor, {"offering_ids": []})

    assert response.status_code == 422, response.text
    assert await _held(db_engine, mentor) == [a]


async def test_an_approved_mentor_holding_only_a_retired_offering_can_resend_empty(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The read shows `[]` once support retires their last offering, so
    resending it beside another edit changes nothing and saves."""
    mentor = await make_public_mentor(db_engine, "off-clear-retired")
    retired = await _retired_offering(db_engine)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM mentor_service_offerings WHERE mentor_user_id = :u"), {"u": mentor}
        )
        await conn.execute(
            text(
                "INSERT INTO mentor_service_offerings (mentor_user_id, service_offering_id) "
                "VALUES (:u, :o)"
            ),
            {"u": mentor, "o": retired},
        )

    response = await _patch(
        api_client, db_engine, mentor, {"offering_ids": [], "headline": "Still here"}
    )

    assert response.status_code == 200, response.text
    assert await _headline(db_engine, mentor) == "Still here"


async def test_a_draft_mentor_may_clear_their_help_areas(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Before applying, an empty list is a step in filling the form in; the
    application's own check asks for one at submit."""
    mentor = await make_applicant(db_engine, "off-draft-clear", complete=False)
    (a,) = await _offerings(db_engine, 1)
    assert (await _patch(api_client, db_engine, mentor, {"offering_ids": [a]})).status_code == 200

    response = await _patch(api_client, db_engine, mentor, {"offering_ids": []})

    assert response.status_code == 200, response.text
    assert await _held(db_engine, mentor) == []


@pytest.mark.parametrize("case", ["unknown", "retired", "twice"])
async def test_applying_with_a_bad_offering_creates_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, case: str
) -> None:
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, f"off-apply-{uuid4().hex[:6]}@example.com")
    (known,) = await _offerings(db_engine, 1)
    sent, pointer = {
        "unknown": ([str(uuid4())], "/offering_ids/0"),
        "retired": ([await _retired_offering(db_engine)], "/offering_ids/0"),
        "twice": ([known, known], "/offering_ids"),
    }[case]

    response = await api_client.post(
        url(user_id, "mentor-profile"),
        headers=bearer(api_token(auth_id)),
        json={"headline": "Hi", "offering_ids": sent},
    )

    assert response.status_code == 422, response.text
    assert _pointers(response) == [pointer]
    async with db_engine.connect() as conn:
        made = await conn.execute(
            text("SELECT count(*) FROM mentor_profiles WHERE user_id = :u"), {"u": user_id}
        )
    assert made.scalar_one() == 0
