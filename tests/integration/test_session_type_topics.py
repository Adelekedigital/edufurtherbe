"""A session type covers up to three service offerings (Session Types #9).

Owner decision, 2026-09-28: "yes, several, up to 3". The set lives in
`session_type_offerings`; `service_offering_id` and the single read field stay
for this release, derived as the **first** of the set (expand/contract — the
column goes in a later release).
"""

from __future__ import annotations

from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import add_availability, add_session_type
from tests.integration.test_api_me_session_type_writes import URL, as_mentor, body

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]

TESTS, DOCS, INTERVIEW, SCHOLARSHIPS = (
    "test-preparation",
    "document-preparation",
    "interview-preparation",
    "scholarships-financial-aid",
)


async def offering_ids(engine: AsyncEngine) -> dict[str, str]:
    async with engine.begin() as conn:
        rows = await conn.execute(text("SELECT slug, id FROM service_offerings"))
        return {row.slug: str(row.id) for row in rows}


async def own(client: httpx.AsyncClient, auth: UUID, type_id: str) -> dict[str, object]:
    rows = (await client.get(URL, headers=bearer(api_token(auth)))).json()["data"]
    return next(row for row in rows if row["id"] == type_id)


def codes(row: dict[str, object]) -> list[str]:
    return [o["code"] for o in row["service_offerings"]]  # type: ignore[attr-defined,index]


async def test_a_type_can_cover_several_offerings_in_order(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth = await as_mentor(db_engine, "topics-many")
    ids = await offering_ids(db_engine)

    created = await api_client.post(
        URL,
        json=body(service_offering_ids=[ids[DOCS], ids[SCHOLARSHIPS], ids[TESTS]]),
        headers=bearer(api_token(auth)),
    )
    row = await own(api_client, auth, created.json()["id"])

    assert created.status_code == 201
    assert codes(row) == [DOCS, SCHOLARSHIPS, TESTS]
    # The single field this release keeps is the first of the set.
    assert row["service_offering"]["code"] == DOCS  # type: ignore[index]


async def test_the_public_read_carries_the_set_too(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "topics-public")
    await add_availability(db_engine, mentor)
    ids = await offering_ids(db_engine)
    created = (
        await api_client.post(
            URL,
            json=body(service_offering_ids=[ids[INTERVIEW], ids[DOCS]]),
            headers=bearer(api_token(auth)),
        )
    ).json()

    public = (await api_client.get(f"/api/v1/users/{mentor}/session-types")).json()["data"]
    (row,) = [t for t in public if t["id"] == created["id"]]

    assert codes(row) == [INTERVIEW, DOCS]
    assert row["service_offering"]["code"] == INTERVIEW


async def test_the_single_field_still_writes_a_set_of_one(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth = await as_mentor(db_engine, "topics-legacy")
    ids = await offering_ids(db_engine)

    created = (
        await api_client.post(
            URL, json=body(service_offering_id=ids[TESTS]), headers=bearer(api_token(auth))
        )
    ).json()

    assert codes(await own(api_client, auth, created["id"])) == [TESTS]


async def test_a_type_written_only_to_the_old_column_still_reads(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Old code during a deploy, the demo seed and the factories write only
    `service_offering_id`. Reads fall back to it rather than showing nothing."""
    mentor, auth = await as_mentor(db_engine, "topics-fallback")
    type_id = await add_session_type(db_engine, mentor, service_offering=SCHOLARSHIPS)

    row = await own(api_client, auth, str(type_id))

    assert codes(row) == [SCHOLARSHIPS]
    assert row["service_offering"]["code"] == SCHOLARSHIPS  # type: ignore[index]


async def test_an_unclassified_type_has_an_empty_set(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth = await as_mentor(db_engine, "topics-none")

    created = (await api_client.post(URL, json=body(), headers=bearer(api_token(auth)))).json()
    row = await own(api_client, auth, created["id"])

    assert row["service_offerings"] == []
    assert row["service_offering"] is None


@pytest.mark.parametrize(
    "case",
    ["four", "duplicate", "unknown", "both-fields"],
)
async def test_a_bad_set_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, case: str
) -> None:
    mentor, auth = await as_mentor(db_engine, f"topics-bad-{case}")
    ids = await offering_ids(db_engine)
    payload = {
        "four": body(
            service_offering_ids=[ids[TESTS], ids[DOCS], ids[INTERVIEW], ids[SCHOLARSHIPS]]
        ),
        "duplicate": body(service_offering_ids=[ids[TESTS], ids[TESTS]]),
        "unknown": body(service_offering_ids=["019ffbe6-8cfc-759b-aaf9-bb480b8c29ad"]),
        "both-fields": body(service_offering_ids=[ids[TESTS]], service_offering_id=ids[DOCS]),
    }[case]

    response = await api_client.post(URL, json=payload, headers=bearer(api_token(auth)))

    assert response.status_code == 422, response.text
    async with db_engine.begin() as conn:
        written = await conn.execute(
            text("SELECT count(*) FROM session_types WHERE mentor_user_id = :u"), {"u": mentor}
        )
    assert written.scalar_one() == 0


async def test_a_retired_offering_cannot_be_chosen(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth = await as_mentor(db_engine, "topics-retired")
    ids = await offering_ids(db_engine)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE service_offerings SET is_active = false WHERE slug = :s"), {"s": DOCS}
        )

    response = await api_client.post(
        URL, json=body(service_offering_ids=[ids[DOCS]]), headers=bearer(api_token(auth))
    )

    assert response.status_code == 422


async def test_patching_replaces_the_set_and_empty_clears_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth = await as_mentor(db_engine, "topics-patch")
    ids = await offering_ids(db_engine)
    headers = bearer(api_token(auth))
    created = (
        await api_client.post(URL, json=body(service_offering_ids=[ids[TESTS]]), headers=headers)
    ).json()
    path = f"{URL}/{created['id']}"

    await api_client.patch(
        path, json={"service_offering_ids": [ids[INTERVIEW], ids[TESTS]]}, headers=headers
    )
    replaced = await own(api_client, auth, created["id"])
    await api_client.patch(path, json={"service_offering_ids": []}, headers=headers)
    cleared = await own(api_client, auth, created["id"])

    assert codes(replaced) == [INTERVIEW, TESTS]
    assert replaced["service_offering"]["code"] == INTERVIEW  # type: ignore[index]
    assert cleared["service_offerings"] == []
    assert cleared["service_offering"] is None


async def test_patching_the_single_field_sets_a_set_of_one(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth = await as_mentor(db_engine, "topics-patch-legacy")
    ids = await offering_ids(db_engine)
    headers = bearer(api_token(auth))
    created = (
        await api_client.post(
            URL, json=body(service_offering_ids=[ids[TESTS], ids[DOCS]]), headers=headers
        )
    ).json()

    await api_client.patch(
        f"{URL}/{created['id']}", json={"service_offering_id": ids[INTERVIEW]}, headers=headers
    )

    assert codes(await own(api_client, auth, created["id"])) == [INTERVIEW]


async def test_a_patch_that_leaves_the_set_out_keeps_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth = await as_mentor(db_engine, "topics-keep")
    ids = await offering_ids(db_engine)
    headers = bearer(api_token(auth))
    created = (
        await api_client.post(
            URL, json=body(service_offering_ids=[ids[TESTS], ids[DOCS]]), headers=headers
        )
    ).json()

    await api_client.patch(f"{URL}/{created['id']}", json={"name": "Renamed"}, headers=headers)

    assert codes(await own(api_client, auth, created["id"])) == [TESTS, DOCS]


async def test_the_old_column_holds_the_first_of_the_set(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The expand step: code from before this release reads only
    `session_types.service_offering_id`, so it must hold the set's first — the
    API cannot show this, because it derives the single field from the set."""
    _, auth = await as_mentor(db_engine, "topics-dual")
    ids = await offering_ids(db_engine)
    headers = bearer(api_token(auth))
    created = (
        await api_client.post(
            URL, json=body(service_offering_ids=[ids[DOCS], ids[TESTS]]), headers=headers
        )
    ).json()

    async def column() -> str | None:
        async with db_engine.begin() as conn:
            value = (
                await conn.execute(
                    text("SELECT service_offering_id FROM session_types WHERE id = :t"),
                    {"t": created["id"]},
                )
            ).scalar_one()
        return None if value is None else str(value)

    first = await column()
    await api_client.patch(
        f"{URL}/{created['id']}", json={"service_offering_ids": []}, headers=headers
    )
    cleared = await column()

    assert first == ids[DOCS]
    assert cleared is None
