"""The two columns that stopped being free text.

`session_types.category` and `application_stage` shipped with no constraint and
no value in any row, withheld from the public contract because publishing them
*"would commit this contract to a shape nobody has designed"*. Both have a shape
now: a reference to the closed six-row `service_offerings` taxonomy, and a
five-value closed set.

**The symmetric `CHECK` is the interesting one.** `other` carries a label and no
other value does. The half a one-directional form misses is **`other` with no
label** — `custom_url IS NULL OR provider = 'custom'` is satisfied by a null, so
it never required the payload it exists to require. That is the shape
`ck_mentor_profiles_custom_url_requires_custom_venue` had, and it is why a mentor
could be on a custom venue with nowhere to meet.

It is enforced twice on purpose: at the boundary so a mismatch is a `422`
naming the field, and in the database so it holds for every writer including
the ETL and `psql`.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import add_session_type, make_public_mentor

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

URL = "/api/v1/me/session-types"


async def as_mentor(engine: AsyncEngine, tag: str) -> tuple[UUID, UUID]:
    mentor = await make_public_mentor(engine, tag)
    auth_id = uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET auth_id = :a WHERE id = :u"), {"a": auth_id, "u": mentor}
        )
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


async def test_other_without_a_label_is_refused(db_engine: AsyncEngine) -> None:
    """**The direction a one-directional constraint misses**, verified by
    breaking it: rewriting the `CHECK` as
    `custom_stage_label IS NULL OR application_stage = 'other'` — the shape
    `ck_mentor_profiles_custom_url_requires_custom_venue` had — fails only this
    test. `other` with a null label satisfies the first clause and slips through,
    which is the same hole that let a mentor be on a custom venue with no URL.

    What it leaves behind is a blank chip: a stage whose whole meaning is the
    label, rendering nothing.
    """
    mentor = await make_public_mentor(db_engine, "other-no-label")
    session_type = await add_session_type(db_engine, mentor)

    with pytest.raises(IntegrityError):
        await write_stage(db_engine, session_type, "other", None)


async def test_a_label_on_a_named_stage_is_refused(db_engine: AsyncEngine) -> None:
    """The other half. A one-directional constraint *does* catch this one — the
    probe above confirms it — so this test is not what the symmetry buys.

    It earns its place anyway: a mentor picks `other`, types a label, then
    changes to `revisions`, and without a constraint in this direction the label
    survives as dead data that renders nowhere and reappears the day something
    starts reading it. That is how `custom_meeting_url` outlived the venue it
    belonged to.
    """
    mentor = await make_public_mentor(db_engine, "label-no-other")
    session_type = await add_session_type(db_engine, mentor)

    with pytest.raises(IntegrityError):
        await write_stage(db_engine, session_type, "revisions", "left over")


async def test_an_offering_cannot_point_at_a_service_offering_that_is_not_there(
    db_engine: AsyncEngine,
) -> None:
    """The foreign key, asserted rather than assumed. Free text accepted anything;
    a reference must not."""
    mentor = await make_public_mentor(db_engine, "bad-offering")
    session_type = await add_session_type(db_engine, mentor)

    with pytest.raises(IntegrityError):
        async with db_engine.begin() as conn:
            await conn.execute(
                text("UPDATE session_types SET service_offering_id = :s WHERE id = :t"),
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
    [("other", None), ("revisions", "left over")],
)
async def test_a_mismatched_label_is_a_422(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, stage: str, label: str | None
) -> None:
    """Both directions at the boundary, mirroring the `CHECK`.

    One shared validator serves the create and patch models, because the rule is
    one rule and a copy in each is the duplication that stops matching the
    database the day one is edited.
    """
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


async def test_a_patch_naming_only_the_label_is_left_to_the_database(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The one case the boundary deliberately does not judge.**

    On a `PATCH` an absent field means *leave it alone*, so a request naming only
    `custom_stage_label` gives the validator nothing to compare against. Judging
    it anyway would refuse a legal edit — setting the label on an offering that
    is already `other`. So it is allowed through, and the row it would produce is
    still refused by the `CHECK`.

    This asserts the legal edit succeeds. The illegal one reaching the database
    is the accepted cost of not refusing this one.
    """
    mentor, auth_id = await as_mentor(db_engine, "patch-label-only")
    session_type = await add_session_type(db_engine, mentor, application_stage="other")

    changed = await api_client.patch(
        f"{URL}/{session_type}",
        json={"custom_stage_label": "Reworded"},
        headers=bearer(api_token(auth_id)),
    )

    assert changed.status_code == 200, changed.text
    (offering,) = (await api_client.get(URL, headers=bearer(api_token(auth_id)))).json()["data"]
    assert offering["custom_stage_label"] == "Reworded"


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

    expected = {"code": "interview-preparation", "display_name": "Interview Preparation"}
    assert owner[0]["service_offering"] == expected
    assert public[0]["service_offering"] == expected


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
    assert owner[0]["service_offering"] is None
    assert public[0]["service_offering"] is None
