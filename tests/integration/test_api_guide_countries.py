"""The countries a mentor can guide mentees towards (decision 276).

Written as one list on the mentor-profile write and read back, in the mentor's
order, on the owner's read, `/me` and the public profile. The schema caps the
list at `MAX_GUIDE_COUNTRIES`; the boundary refuses a ninth, a repeat, an
explicit `null` and an id that names no country, each a `422` with a pointer,
before anything is written.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import make_public_mentor, token_for
from tests.integration.test_api_writes import make_user, url

from app.domain.guide_countries import MAX_GUIDE_COUNTRIES
from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def _countries(engine: AsyncEngine, count: int) -> list[dict[str, Any]]:
    """`count` catalogue countries, as the reads publish them."""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT id, code, display_name FROM countries ORDER BY code LIMIT :n"),
                {"n": count},
            )
        ).all()
    return [{"id": str(r.id), "code": r.code.strip(), "display_name": r.display_name} for r in rows]


async def _mentor(engine: AsyncEngine, tag: str, **knobs: Any) -> tuple[UUID, dict[str, str]]:
    mentor = await make_public_mentor(engine, tag, **knobs)
    return mentor, bearer(await token_for(engine, mentor))


async def _patch(
    client: httpx.AsyncClient, user: UUID, headers: dict[str, str], body: dict[str, Any]
) -> httpx.Response:
    return await client.patch(url(user, "mentor-profile"), headers=headers, json=body)


async def _owner_read(
    client: httpx.AsyncClient, user: UUID, headers: dict[str, str]
) -> list[dict[str, Any]]:
    response = await client.get(url(user, "mentor-profile"), headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["guide_countries"]  # type: ignore[no-any-return]


async def _me_read(client: httpx.AsyncClient, headers: dict[str, str]) -> list[dict[str, Any]]:
    response = await client.get("/api/v1/me", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["mentor_profile"]["guide_countries"]  # type: ignore[no-any-return]


async def _public_read(client: httpx.AsyncClient, user: UUID) -> list[dict[str, Any]]:
    response = await client.get(f"/api/v1/mentors/{user}")
    assert response.status_code == 200, response.text
    return response.json()["guide_countries"]  # type: ignore[no-any-return]


async def _stored(engine: AsyncEngine, user: UUID) -> list[tuple[str, int]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT country_id, position FROM mentor_guide_countries "
                "WHERE mentor_user_id = :u ORDER BY position"
            ),
            {"u": user},
        )
        return [(str(r.country_id), r.position) for r in rows]


def _ids(countries: list[dict[str, Any]]) -> list[str]:
    return [c["id"] for c in countries]


def _pointers(response: httpx.Response) -> list[str]:
    return [e["pointer"] for e in response.json()["errors"]]


async def test_a_saved_list_reads_back_in_order_on_every_read(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, headers = await _mentor(db_engine, "guide-set")
    a, b, c = await _countries(db_engine, 3)
    sent = [c, a, b]  # not catalogue order, so the order read back is the mentor's

    saved = await _patch(api_client, mentor, headers, {"guide_country_ids": _ids(sent)})

    assert saved.status_code == 200, saved.text
    owner = await _owner_read(api_client, mentor, headers)
    assert owner == sent
    # One store function behind all three, so they cannot disagree.
    assert await _me_read(api_client, headers) == owner
    assert await _public_read(api_client, mentor) == owner
    assert await _stored(db_engine, mentor) == [(x["id"], i) for i, x in enumerate(sent)]


async def test_a_new_list_replaces_and_reorders_the_old(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, headers = await _mentor(db_engine, "guide-replace")
    a, b, c, d = await _countries(db_engine, 4)
    await _patch(api_client, mentor, headers, {"guide_country_ids": _ids([a, b, c])})

    # Reorder two kept ones and swap the third out: every position moves.
    saved = await _patch(api_client, mentor, headers, {"guide_country_ids": _ids([b, d, a])})

    assert saved.status_code == 200, saved.text
    assert await _owner_read(api_client, mentor, headers) == [b, d, a]


async def test_an_empty_list_clears_and_an_absent_one_leaves(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, headers = await _mentor(db_engine, "guide-clear")
    a, b = await _countries(db_engine, 2)
    await _patch(api_client, mentor, headers, {"guide_country_ids": _ids([a, b])})

    other_edit = await _patch(api_client, mentor, headers, {"headline": "Still here"})
    assert other_edit.status_code == 200, other_edit.text
    assert await _owner_read(api_client, mentor, headers) == [a, b]

    cleared = await _patch(api_client, mentor, headers, {"guide_country_ids": []})
    assert cleared.status_code == 200, cleared.text
    assert await _owner_read(api_client, mentor, headers) == []
    assert await _public_read(api_client, mentor) == []


async def test_a_mentor_who_never_named_any_reads_an_empty_list(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, headers = await _mentor(db_engine, "guide-none")

    assert await _owner_read(api_client, mentor, headers) == []
    assert await _me_read(api_client, headers) == []
    assert await _public_read(api_client, mentor) == []


async def test_exactly_the_maximum_is_accepted(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, headers = await _mentor(db_engine, "guide-max")
    every = await _countries(db_engine, MAX_GUIDE_COUNTRIES)

    saved = await _patch(api_client, mentor, headers, {"guide_country_ids": _ids(every)})

    assert saved.status_code == 200, saved.text
    assert await _owner_read(api_client, mentor, headers) == every


async def test_one_over_the_maximum_is_a_422_at_the_list(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, headers = await _mentor(db_engine, "guide-over")
    every = await _countries(db_engine, MAX_GUIDE_COUNTRIES + 1)

    refused = await _patch(api_client, mentor, headers, {"guide_country_ids": _ids(every)})

    assert refused.status_code == 422, refused.text
    assert _pointers(refused) == ["/guide_country_ids"]
    assert await _stored(db_engine, mentor) == []


async def test_a_repeated_country_is_a_422_at_the_list(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, headers = await _mentor(db_engine, "guide-dup")
    a, b = await _countries(db_engine, 2)

    refused = await _patch(api_client, mentor, headers, {"guide_country_ids": _ids([a, b, a])})

    assert refused.status_code == 422, refused.text
    assert _pointers(refused) == ["/guide_country_ids"]
    assert await _stored(db_engine, mentor) == []


async def test_an_explicit_null_is_a_422_and_leaves_the_list(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, headers = await _mentor(db_engine, "guide-null")
    (a,) = await _countries(db_engine, 1)
    await _patch(api_client, mentor, headers, {"guide_country_ids": [a["id"]]})

    refused = await _patch(api_client, mentor, headers, {"guide_country_ids": None})

    assert refused.status_code == 422, refused.text
    assert _pointers(refused) == ["/guide_country_ids"]
    assert await _owner_read(api_client, mentor, headers) == [a]


async def test_an_unknown_country_is_a_422_at_its_index_and_writes_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, headers = await _mentor(db_engine, "guide-unknown")
    a, b = await _countries(db_engine, 2)
    await _patch(api_client, mentor, headers, {"guide_country_ids": [a["id"]]})

    refused = await _patch(
        api_client,
        mentor,
        headers,
        {"guide_country_ids": [b["id"], str(uuid4())], "headline": "Not saved"},
    )

    assert refused.status_code == 422, refused.text
    assert _pointers(refused) == ["/guide_country_ids/1"]
    assert await _owner_read(api_client, mentor, headers) == [a]
    async with db_engine.connect() as conn:
        headline = (
            await conn.execute(
                text("SELECT headline FROM mentor_profiles WHERE user_id = :u"), {"u": mentor}
            )
        ).scalar_one()
    assert headline == "M"


async def test_another_users_write_is_refused_and_writes_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    victim, victim_headers = await _mentor(db_engine, "guide-victim")
    _, intruder_headers = await _mentor(db_engine, "guide-intruder")
    a, b = await _countries(db_engine, 2)
    await _patch(api_client, victim, victim_headers, {"guide_country_ids": [a["id"]]})

    refused = await _patch(api_client, victim, intruder_headers, {"guide_country_ids": [b["id"]]})

    assert refused.status_code == 404, refused.text
    assert await _owner_read(api_client, victim, victim_headers) == [a]


async def test_an_admin_does_not_write_them(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Decision 66: the admin reads the owner's view and never writes it."""
    mentor, headers = await _mentor(db_engine, "guide-admin-target")
    admin_auth = uuid4()
    await make_user(db_engine, admin_auth, "guide-admin@example.com", admin=True)
    admin = bearer(api_token(admin_auth))
    (a,) = await _countries(db_engine, 1)
    await _patch(api_client, mentor, headers, {"guide_country_ids": [a["id"]]})

    refused = await _patch(api_client, mentor, admin, {"guide_country_ids": []})

    assert refused.status_code == 404, refused.text
    assert await _owner_read(api_client, mentor, admin) == [a]


async def test_a_non_mentor_gets_the_endpoints_404(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    user = await make_user(db_engine, auth_id, "guide-mentee@example.com")
    (a,) = await _countries(db_engine, 1)

    refused = await _patch(
        api_client, user, bearer(api_token(auth_id)), {"guide_country_ids": [a["id"]]}
    )

    assert refused.status_code == 404, refused.text


async def test_applying_with_a_list_saves_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`MentorProfileWrite` is the create body too, so the list is accepted there."""
    auth_id = uuid4()
    user = await make_user(db_engine, auth_id, "guide-apply@example.com")
    headers = bearer(api_token(auth_id))
    a, b = await _countries(db_engine, 2)

    created = await api_client.post(
        url(user, "mentor-profile"), headers=headers, json={"guide_country_ids": _ids([b, a])}
    )

    assert created.status_code == 201, created.text
    assert await _owner_read(api_client, user, headers) == [b, a]


async def test_applying_with_an_unknown_country_creates_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    user = await make_user(db_engine, auth_id, "guide-apply-unknown@example.com")
    headers = bearer(api_token(auth_id))

    refused = await api_client.post(
        url(user, "mentor-profile"), headers=headers, json={"guide_country_ids": [str(uuid4())]}
    )

    assert refused.status_code == 422, refused.text
    assert _pointers(refused) == ["/guide_country_ids/0"]
    gone = await api_client.get(url(user, "mentor-profile"), headers=headers)
    assert gone.status_code == 404, gone.text


async def test_a_hidden_mentors_list_stays_hidden_from_strangers(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, headers = await _mentor(db_engine, "guide-hidden", listed=False)
    (a,) = await _countries(db_engine, 1)
    await _patch(api_client, mentor, headers, {"guide_country_ids": [a["id"]]})

    stranger = await api_client.get(f"/api/v1/mentors/{mentor}")
    owner = await api_client.get(f"/api/v1/mentors/{mentor}", headers=headers)

    assert stranger.status_code == 404, stranger.text
    assert "guide_countries" not in stranger.text
    assert owner.status_code == 200, owner.text
    assert owner.json()["guide_countries"] == [a]


async def test_an_unknown_study_country_is_a_422_beside_them(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The same check covers the mentor profile's other country reference, which
    reached the foreign key as a 500 before this list shared its query."""
    mentor, headers = await _mentor(db_engine, "guide-study")
    (a,) = await _countries(db_engine, 1)

    refused = await _patch(
        api_client,
        mentor,
        headers,
        {"primary_study_country_id": str(uuid4()), "guide_country_ids": [a["id"], str(uuid4())]},
    )

    assert refused.status_code == 422, refused.text
    assert sorted(_pointers(refused)) == ["/guide_country_ids/1", "/primary_study_country_id"]
    assert await _stored(db_engine, mentor) == []


async def test_replacements_arriving_together_each_land_whole(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`lock_mentor` serialises them; without it the later insert meets the
    earlier one's rows on the unique `position` and answers 500."""
    mentor, headers = await _mentor(db_engine, "guide-race")
    every = await _countries(db_engine, 6)
    lists = [_ids(every[i : i + 3]) for i in range(4)]

    answers = await asyncio.gather(
        *(_patch(api_client, mentor, headers, {"guide_country_ids": ids}) for ids in lists)
    )

    assert [a.status_code for a in answers] == [200] * len(lists), [a.text for a in answers]
    stored = await _stored(db_engine, mentor)
    assert [country for country, _ in stored] in lists
    assert [position for _, position in stored] == [0, 1, 2]


async def test_one_mentors_list_never_touches_anothers(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    first, first_headers = await _mentor(db_engine, "guide-first")
    second, second_headers = await _mentor(db_engine, "guide-second")
    a, b, c = await _countries(db_engine, 3)
    await _patch(api_client, second, second_headers, {"guide_country_ids": _ids([c, a])})

    await _patch(api_client, first, first_headers, {"guide_country_ids": _ids([a, b])})
    await _patch(api_client, first, first_headers, {"guide_country_ids": [b["id"]]})

    assert await _owner_read(api_client, first, first_headers) == [b]
    assert await _owner_read(api_client, second, second_headers) == [c, a]
    assert await _public_read(api_client, second) == [c, a]
