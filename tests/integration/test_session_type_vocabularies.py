"""The two columns that stopped being free text.

`session_types.category` and `application_stage` shipped with no constraint and
no value in any row, withheld from the public contract because publishing them
*"would commit this contract to a shape nobody has designed"*. Both have a shape
now: a reference to the closed six-row `service_offerings` taxonomy, and the
design's six stages with no custom stage (decision 288).

Enforced twice on purpose: at the boundary so a bad value is a `422` naming the
field, and in the database so it holds for every writer including the ETL and
`psql`.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import add_session_type, make_public_mentor, token_for

from conftest import api_token, bearer


async def offering_id(engine: AsyncEngine, slug: str) -> str:
    """The catalogue id a `LookupRef` carries, so a form can write it back."""
    async with engine.connect() as conn:
        return str(
            (
                await conn.execute(
                    text("SELECT id FROM service_offerings WHERE slug = :s"), {"s": slug}
                )
            ).scalar_one()
        )


pytestmark = [pytest.mark.db, pytest.mark.asyncio]

URL = "/api/v1/me/session-types"


async def as_mentor(engine: AsyncEngine, tag: str) -> tuple[UUID, UUID]:
    mentor = await make_public_mentor(engine, tag)
    auth_id = uuid4()
    await token_for(engine, mentor, auth_id=auth_id)
    return mentor, auth_id


async def write_stage(
    engine: AsyncEngine, session_type: UUID, stage: str | None, label: str | None
) -> None:
    """Straight to the column, bypassing every boundary — which is the point.

    The `CHECK` has to hold for the ETL and for `psql`, not only for requests
    that came through Pydantic.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE session_types "
                "SET application_stage = :s, custom_stage_label = :l WHERE id = :t"
            ),
            {"s": stage, "l": label, "t": session_type},
        )


# --------------------------------------------------------------------------
# The database half
# --------------------------------------------------------------------------


async def test_an_unknown_stage_is_refused(db_engine: AsyncEngine) -> None:
    """A closed set, and the `CHECK` is what makes it closed for the ETL too.

    `postgrad` is the value the old fixtures used while the column was free
    text — the most likely thing to arrive from a transform written against the
    previous shape.
    """
    mentor = await make_public_mentor(db_engine, "bad-stage")
    session_type = await add_session_type(db_engine, mentor)

    with pytest.raises(IntegrityError):
        await write_stage(db_engine, session_type, "postgrad", None)


async def test_a_null_stage_is_permitted(db_engine: AsyncEngine) -> None:
    """The column is nullable and an offering aimed at no particular stage is
    ordinary — so the `CHECK` reads `IS NULL OR ...` rather than assuming a
    value. A constraint written without it would refuse every offering that
    exists today."""
    mentor = await make_public_mentor(db_engine, "null-stage")
    session_type = await add_session_type(db_engine, mentor)

    await write_stage(db_engine, session_type, None, None)


@pytest.mark.parametrize(
    "stage",
    [
        "early_exploration",
        "drafting_stage",
        "post_submission",
        "revisions",
        "interviewing",
        "other",
    ],
)
async def test_a_retired_stage_is_refused_by_the_database(
    db_engine: AsyncEngine, stage: str
) -> None:
    """The contract migration narrowed the `CHECK` to the six (decision 288)."""
    mentor = await make_public_mentor(db_engine, f"retired-{stage}")
    session_type = await add_session_type(db_engine, mentor)

    with pytest.raises(IntegrityError):
        await write_stage(db_engine, session_type, stage, None)


async def test_any_label_is_refused_by_the_database(db_engine: AsyncEngine) -> None:
    """No custom stage, so the column holds nothing, for every writer."""
    mentor = await make_public_mentor(db_engine, "label-refused")
    session_type = await add_session_type(db_engine, mentor)

    with pytest.raises(IntegrityError):
        await write_stage(db_engine, session_type, "applying", "beside a stage")


async def test_an_offering_cannot_point_at_a_service_offering_that_is_not_there(
    db_engine: AsyncEngine,
) -> None:
    """The foreign key, asserted rather than assumed. Free text accepted anything;
    a reference must not. On the set, where a type's offerings live (#274)."""
    mentor = await make_public_mentor(db_engine, "bad-offering")
    session_type = await add_session_type(db_engine, mentor)

    with pytest.raises(IntegrityError):
        async with db_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO session_type_offerings "
                    "(session_type_id, service_offering_id, position) VALUES (:t, :s, 0)"
                ),
                {"s": uuid4(), "t": session_type},
            )


# --------------------------------------------------------------------------
# The boundary half
# --------------------------------------------------------------------------


async def test_an_unknown_stage_is_a_422_not_a_500(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The `CHECK` would refuse this too — as a 500 naming a constraint. The
    boundary turns it into a 422 naming the field."""
    _, auth_id = await as_mentor(db_engine, "api-bad-stage")

    refused = await api_client.post(
        URL,
        json={"name": "X", "duration_minutes": 45, "application_stage": "postgrad"},
        headers=bearer(api_token(auth_id)),
    )

    assert refused.status_code == 422, refused.text


@pytest.mark.parametrize(
    ("stage", "label"),
    [("other", None), ("applying", "left over")],
)
async def test_a_mismatched_label_is_a_422(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, stage: str, label: str | None
) -> None:
    """`other` is no stage, and no label may be written (decision 288)."""
    _, auth_id = await as_mentor(db_engine, f"api-mismatch-{stage}")

    refused = await api_client.post(
        URL,
        json={
            "name": "X",
            "duration_minutes": 45,
            "application_stage": stage,
            "custom_stage_label": label,
        },
        headers=bearer(api_token(auth_id)),
    )

    assert refused.status_code == 422, refused.text


async def test_classifying_an_offering_shows_the_taxonomy_on_both_reads(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """One join, attached by one helper, so the two lists cannot drift.

    The owner list and the public list select different column sets and are the
    classic place for a field to reach one and not the other.
    """
    mentor, auth_id = await as_mentor(db_engine, "classified")
    await add_session_type(db_engine, mentor, service_offering="interview-preparation")

    owner = (await api_client.get(URL, headers=bearer(api_token(auth_id)))).json()["data"]
    public = (await api_client.get(f"/api/v1/users/{mentor}/session-types")).json()["data"]

    expected = {
        "id": await offering_id(db_engine, "interview-preparation"),
        "code": "interview-preparation",
        "display_name": "Interview preparation",
    }
    assert owner[0]["service_offerings"] == [expected]
    assert public[0]["service_offerings"] == [expected]


async def test_an_unclassified_offering_is_not_an_error(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The join is **outer**, and every offering is unclassified today.

    An inner join would silently drop the entire population from both lists
    while every test that seeds a category kept passing.
    """
    mentor, auth_id = await as_mentor(db_engine, "unclassified")
    await add_session_type(db_engine, mentor)

    owner = (await api_client.get(URL, headers=bearer(api_token(auth_id)))).json()["data"]
    public = (await api_client.get(f"/api/v1/users/{mentor}/session-types")).json()["data"]

    assert len(owner) == 1
    assert len(public) == 1
    assert owner[0]["service_offerings"] == []
    assert public[0]["service_offerings"] == []
