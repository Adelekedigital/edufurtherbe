"""A session type is aimed at several application stages (#212).

Session Types frontend round 3 A, approved by the product owner 2026-09-29: "Best
for mentees who are…" became "Pick all that apply". The set lives in
`session_type_stages`; `application_stage` stays for this release as the
**first** of the set (expand/contract, the #205 shape), and `custom_stage_label`
belongs to the set whenever it holds `other`.
"""

from __future__ import annotations

from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import add_session_type
from tests.integration.test_api_me_session_type_writes import URL, as_mentor, body

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]

DRAFTING, INTERVIEWING, REVISIONS, OTHER = "drafting_stage", "interviewing", "revisions", "other"


async def own(client: httpx.AsyncClient, auth: UUID, type_id: str) -> dict[str, object]:
    rows = (await client.get(URL, headers=bearer(api_token(auth)))).json()["data"]
    return next(row for row in rows if row["id"] == type_id)


async def create(client: httpx.AsyncClient, auth: UUID, **fields: object) -> httpx.Response:
    return await client.post(URL, json=body(**fields), headers=bearer(api_token(auth)))


async def test_a_type_can_be_aimed_at_several_stages_in_order(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth = await as_mentor(db_engine, "stages-many")

    created = await create(api_client, auth, application_stages=[INTERVIEWING, DRAFTING])
    row = await own(api_client, auth, created.json()["id"])

    assert created.status_code == 201, created.text
    assert row["application_stages"] == [INTERVIEWING, DRAFTING]
    # The single field this release keeps is the first of the set.
    assert row["application_stage"] == INTERVIEWING
    assert row["custom_stage_label"] is None


async def test_other_anywhere_in_the_set_carries_the_label(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`other` behind a named stage: legal, and the case the symmetric `CHECK`
    on the first stage would have refused."""
    mentor, auth = await as_mentor(db_engine, "stages-other-second")

    created = await create(
        api_client, auth, application_stages=[DRAFTING, OTHER], custom_stage_label="Gap year"
    )
    row = await own(api_client, auth, created.json()["id"])
    public = (await api_client.get(f"/api/v1/users/{mentor}/session-types")).json()["data"]

    assert created.status_code == 201, created.text
    assert row["application_stages"] == [DRAFTING, OTHER]
    assert row["custom_stage_label"] == "Gap year"
    (listed,) = [t for t in public if t["id"] == created.json()["id"]]
    assert listed["application_stages"] == [DRAFTING, OTHER]
    assert listed["custom_stage_label"] == "Gap year"


async def test_an_empty_set_means_any_stage(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth = await as_mentor(db_engine, "stages-none")

    created = await create(api_client, auth, application_stages=[])
    row = await own(api_client, auth, created.json()["id"])

    assert created.status_code == 201
    assert row["application_stages"] == []
    assert row["application_stage"] is None


async def test_the_single_field_still_writes_a_set_of_one(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth = await as_mentor(db_engine, "stages-legacy")

    created = await create(api_client, auth, application_stage=REVISIONS)

    assert (await own(api_client, auth, created.json()["id"]))["application_stages"] == [REVISIONS]


async def test_a_type_written_only_to_the_old_column_still_reads(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Old code during the deploy, the demo seed and the factories write only
    `application_stage`. Reads fall back to it rather than showing any stage."""
    mentor, auth = await as_mentor(db_engine, "stages-fallback")
    type_id = await add_session_type(db_engine, mentor, application_stage=REVISIONS)

    row = await own(api_client, auth, str(type_id))

    assert row["application_stages"] == [REVISIONS]
    assert row["application_stage"] == REVISIONS


@pytest.mark.parametrize(
    ("case", "payload"),
    [
        ("duplicate", {"application_stages": [DRAFTING, DRAFTING]}),
        ("both-fields", {"application_stages": [DRAFTING], "application_stage": REVISIONS}),
        ("other-without-label", {"application_stages": [DRAFTING, OTHER]}),
        (
            "label-without-other",
            {"application_stages": [DRAFTING], "custom_stage_label": "stray"},
        ),
        ("unknown", {"application_stages": ["someday"]}),
    ],
)
async def test_a_bad_set_is_refused_and_writes_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, case: str, payload: dict[str, object]
) -> None:
    mentor, auth = await as_mentor(db_engine, f"stages-bad-{case}")

    response = await create(api_client, auth, **payload)

    assert response.status_code == 422, response.text
    async with db_engine.begin() as conn:
        written = await conn.execute(
            text("SELECT count(*) FROM session_types WHERE mentor_user_id = :u"), {"u": mentor}
        )
    assert written.scalar_one() == 0


async def test_patching_replaces_the_set_and_empty_clears_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth = await as_mentor(db_engine, "stages-patch")
    headers = bearer(api_token(auth))
    created = (await create(api_client, auth, application_stages=[DRAFTING])).json()
    path = f"{URL}/{created['id']}"

    first = await api_client.patch(
        path, json={"application_stages": [REVISIONS, INTERVIEWING]}, headers=headers
    )
    replaced = await own(api_client, auth, created["id"])
    await api_client.patch(path, json={"application_stages": []}, headers=headers)
    cleared = await own(api_client, auth, created["id"])

    assert first.status_code == 200, first.text
    assert replaced["application_stages"] == [REVISIONS, INTERVIEWING]
    assert replaced["application_stage"] == REVISIONS
    assert cleared["application_stages"] == []
    assert cleared["application_stage"] is None


async def test_a_patch_that_leaves_the_set_out_keeps_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth = await as_mentor(db_engine, "stages-keep")
    created = (await create(api_client, auth, application_stages=[DRAFTING, REVISIONS])).json()

    await api_client.patch(
        f"{URL}/{created['id']}", json={"name": "Renamed"}, headers=bearer(api_token(auth))
    )

    assert (await own(api_client, auth, created["id"]))["application_stages"] == [
        DRAFTING,
        REVISIONS,
    ]


async def test_moving_off_other_without_clearing_the_label_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The dead-label case, judged against the row's final state.** The PATCH
    names only the set; the label it would strand is already stored, which the
    boundary cannot see and the database's one-way `CHECK` would accept."""
    _, auth = await as_mentor(db_engine, "stages-strand")
    headers = bearer(api_token(auth))
    created = (
        await create(
            api_client, auth, application_stages=[DRAFTING, OTHER], custom_stage_label="Gap year"
        )
    ).json()

    stranded = await api_client.patch(
        f"{URL}/{created['id']}", json={"application_stages": [DRAFTING]}, headers=headers
    )
    moved = await api_client.patch(
        f"{URL}/{created['id']}",
        json={"application_stages": [DRAFTING], "custom_stage_label": None},
        headers=headers,
    )

    assert stranded.status_code == 422, stranded.text
    assert stranded.json()["errors"][0]["pointer"] == "/custom_stage_label"
    assert moved.status_code == 200, moved.text
    row = await own(api_client, auth, created["id"])
    assert row["application_stages"] == [DRAFTING]
    assert row["custom_stage_label"] is None


async def test_adding_other_needs_a_label_already_stored_or_sent(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth = await as_mentor(db_engine, "stages-add-other")
    headers = bearer(api_token(auth))
    created = (await create(api_client, auth, application_stages=[DRAFTING])).json()
    path = f"{URL}/{created['id']}"

    bare = await api_client.patch(
        path, json={"application_stages": [DRAFTING, OTHER]}, headers=headers
    )
    labelled = await api_client.patch(
        path,
        json={"application_stages": [DRAFTING, OTHER], "custom_stage_label": "Gap year"},
        headers=headers,
    )
    relabelled = await api_client.patch(
        path, json={"custom_stage_label": "Career change"}, headers=headers
    )

    assert bare.status_code == 422, bare.text
    assert labelled.status_code == 200, labelled.text
    # A label alone is judged against the stored set, which holds `other`.
    assert relabelled.status_code == 200, relabelled.text
    assert (await own(api_client, auth, created["id"]))["custom_stage_label"] == "Career change"


async def test_the_old_column_holds_the_first_of_the_set(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The expand step: code from before this release reads only
    `session_types.application_stage`, so it must hold the set's first — the API
    cannot show this, because it derives the single field from the set."""
    _, auth = await as_mentor(db_engine, "stages-dual")
    headers = bearer(api_token(auth))
    created = (await create(api_client, auth, application_stages=[INTERVIEWING, DRAFTING])).json()

    async def column() -> str | None:
        async with db_engine.begin() as conn:
            value: str | None = (
                await conn.execute(
                    text("SELECT application_stage FROM session_types WHERE id = :t"),
                    {"t": created["id"]},
                )
            ).scalar_one()
        return value

    first = await column()
    await api_client.patch(
        f"{URL}/{created['id']}", json={"application_stages": []}, headers=headers
    )

    assert first == INTERVIEWING
    assert await column() is None


async def test_an_explicit_null_set_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`[]` means any stage and leaving it out means unchanged; `null` would be
    a third spelling of one of them, so it is a 422 rather than a guess."""
    _, auth = await as_mentor(db_engine, "stages-null")
    headers = bearer(api_token(auth))
    created = (await create(api_client, auth, application_stages=[DRAFTING])).json()

    patched = await api_client.patch(
        f"{URL}/{created['id']}", json={"application_stages": None}, headers=headers
    )
    posted = await create(api_client, auth, name="Null set", application_stages=None)

    assert patched.status_code == 422, patched.text
    assert posted.status_code == 422, posted.text
    assert (await own(api_client, auth, created["id"]))["application_stages"] == [DRAFTING]


async def test_a_label_alone_relabels_a_set_holding_other_behind_a_named_stage(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The label is judged against the **stored** set, whose first stage is not
    `other` — the case a check on the first stage alone would refuse."""
    _, auth = await as_mentor(db_engine, "stages-relabel")
    created = (
        await create(
            api_client, auth, application_stages=[DRAFTING, OTHER], custom_stage_label="Gap year"
        )
    ).json()

    response = await api_client.patch(
        f"{URL}/{created['id']}", json={"custom_stage_label": "x"}, headers=bearer(api_token(auth))
    )

    assert response.status_code == 200, response.text
    row = await own(api_client, auth, created["id"])
    assert row["custom_stage_label"] == "x"
    assert row["application_stages"] == [DRAFTING, OTHER]


async def test_a_patch_with_a_stage_twice_is_a_422_not_a_500(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Without a label in the request the boundary does not judge the set, so
    the store's final-state check is what stops the unique index answering."""
    _, auth = await as_mentor(db_engine, "stages-patch-dup")
    created = (await create(api_client, auth, application_stages=[DRAFTING])).json()

    response = await api_client.patch(
        f"{URL}/{created['id']}",
        json={"application_stages": [REVISIONS, REVISIONS]},
        headers=bearer(api_token(auth)),
    )

    assert response.status_code == 422, response.text
    assert (await own(api_client, auth, created["id"]))["application_stages"] == [DRAFTING]
