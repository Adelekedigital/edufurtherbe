"""A session type is aimed at several application stages (#215), from the
design's six (Session Types PRD, founder 2026-10-21).

Session Types frontend round 3 A, approved by the product owner 2026-09-29: "Best
for mentees who are…" became "Pick all that apply". The set lives in
`session_type_stages`; `application_stage` stays for this release as the
**first** of the set (expand/contract, the #205 shape). Since the six there is
no custom stage: `other` and `custom_stage_label` are refused on writes.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.integration.factories import add_session_type
from tests.integration.test_api_me_session_type_writes import URL, as_mentor, body

from app.infra.db.session_type_store import update_session_type
from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]

DRAFTING, INTERVIEWING, REVISIONS = "preparing_documents", "interview_prep", "applying"


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
        ("other", {"application_stages": [DRAFTING, "other"]}),
        ("label", {"application_stages": [DRAFTING], "custom_stage_label": "stray"}),
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


async def test_patching_reorders_the_same_stages(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The same two stages swapped: the delete-then-insert must not meet the
    position or pair index halfway."""
    _, auth = await as_mentor(db_engine, "stages-reorder")
    headers = bearer(api_token(auth))
    created = (await create(api_client, auth, application_stages=[DRAFTING, REVISIONS])).json()

    response = await api_client.patch(
        f"{URL}/{created['id']}",
        json={"application_stages": [REVISIONS, DRAFTING]},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    row = await own(api_client, auth, created["id"])
    assert row["application_stages"] == [REVISIONS, DRAFTING]
    assert row["application_stage"] == REVISIONS


async def test_two_concurrent_edits_of_one_offering_serialise(db_engine: AsyncEngine) -> None:
    """**The row lock in `update_session_type`.** Two edits replacing the set at
    once: without it the second inserts into rows the first has not committed,
    waits on the unique index, and fails with an integrity error once the first
    commits. With it the second waits on the offering row, then replaces the
    committed set — last write wins, no 500."""
    mentor, _ = await as_mentor(db_engine, "stages-race")
    session_type = await add_session_type(db_engine, mentor, application_stage=DRAFTING)

    async with AsyncSession(db_engine) as first, AsyncSession(db_engine) as second:
        await update_session_type(first, mentor, session_type, {"application_stages": [REVISIONS]})
        racing = asyncio.create_task(
            update_session_type(
                second, mentor, session_type, {"application_stages": [INTERVIEWING]}
            )
        )
        done, _ = await asyncio.wait({racing}, timeout=1)
        assert not done, "the second edit did not wait for the first"
        await first.commit()
        assert await racing is True
        await second.commit()

    async with db_engine.begin() as conn:
        stages = [
            r.stage
            for r in await conn.execute(
                text(
                    "SELECT stage FROM session_type_stages WHERE session_type_id = :t "
                    "ORDER BY position"
                ),
                {"t": session_type},
            )
        ]
    assert stages == [INTERVIEWING]


RETIRED = ["early_exploration", "drafting_stage", "revisions", "post_submission", "interviewing"]


@pytest.mark.parametrize("retired", [*RETIRED, "other"])
@pytest.mark.parametrize("field", ["application_stages", "application_stage"])
async def test_a_retired_stage_is_refused_on_every_write(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, retired: str, field: str
) -> None:
    """Only the design's six may be written, through either field."""
    _, auth = await as_mentor(db_engine, f"stages-retired-{field[-1]}-{retired[:6]}")
    value: object = [retired] if field == "application_stages" else retired

    created = await create(api_client, auth, **{field: value})
    kept = (await create(api_client, auth, name="Kept", application_stages=[DRAFTING])).json()
    patched = await api_client.patch(
        f"{URL}/{kept['id']}", json={field: value}, headers=bearer(api_token(auth))
    )

    assert created.status_code == 422, created.text
    assert created.json()["errors"][0]["pointer"].startswith(f"/{field}")
    assert patched.status_code == 422, patched.text
    assert (await own(api_client, auth, kept["id"]))["application_stages"] == [DRAFTING]


async def test_a_custom_label_is_refused_and_null_is_accepted(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth = await as_mentor(db_engine, "stages-label")

    labelled = await create(api_client, auth, custom_stage_label="Gap year")
    nulled = await create(api_client, auth, name="Plain", custom_stage_label=None)

    assert labelled.status_code == 422, labelled.text
    assert labelled.json()["errors"][0]["pointer"] == "/custom_stage_label"
    assert nulled.status_code == 201, nulled.text


async def test_a_null_label_alone_saves(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A client echoing the form back sends the label it read, `null`."""
    _, auth = await as_mentor(db_engine, "stages-null-label")
    created = (await create(api_client, auth, application_stages=[DRAFTING])).json()

    response = await api_client.patch(
        f"{URL}/{created['id']}",
        json={"name": "Echoed", "custom_stage_label": None},
        headers=bearer(api_token(auth)),
    )

    assert response.status_code == 200, response.text
    assert (await own(api_client, auth, created["id"]))["application_stages"] == [DRAFTING]
