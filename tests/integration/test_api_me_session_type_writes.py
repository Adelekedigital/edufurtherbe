"""Creating and changing your own offerings.

**The transaction is the feature.** An offering and its booking config are two
rows, and `/slots` plus both read paths inner-join the config — so an offering
without one is invisible everywhere and unbookable, and nothing writes a config
on its own. There is no endpoint that could repair it. That is why the two
inserts share one commit, and why the second test here is the rollback.

The scope is `session_type_of()`: ownership and soft deletion, **not**
`is_active`. Editing a switched-off offering is the ordinary case and is what
switching it back on requires. The alternative — an `include_inactive` flag on
`session_type_is_live()` — would touch the predicate that decides what is
*bookable*, which `slot_store` spreads, so a mis-defaulted flag would make
deactivated offerings bookable again against settled decision #90.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
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


async def as_mentee(engine: AsyncEngine, tag: str) -> UUID:
    """A user with no mentor profile — the caller a write has no true empty
    answer for."""
    auth_id = uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
                "VALUES (:e, :a, 'Mo', 'mentee', 'UTC')"
            ),
            {"e": f"mentee-{tag}-{uuid4()}@example.test", "a": auth_id},
        )
    return auth_id


def body(**overrides: object) -> dict[str, object]:
    return {"name": "SOP review", "duration_minutes": 45} | overrides


# --------------------------------------------------------------------------
# Create
# --------------------------------------------------------------------------


async def test_creating_writes_the_offering_and_its_config_together(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Both rows, and the offering is immediately readable.

    Reading it back through `/me/session-types` rather than from the table is
    the assertion that matters: the read inner-joins the config, so an offering
    created without one is absent here even though the `POST` answered 201.
    """
    mentor, auth_id = await as_mentor(db_engine, "create")

    created = await api_client.post(URL, json=body(), headers=bearer(api_token(auth_id)))
    assert created.status_code == 201, created.text
    assert created.headers["Location"] == URL

    listed = await api_client.get(URL, headers=bearer(api_token(auth_id)))
    (offering,) = listed.json()["data"]
    assert offering["id"] == created.json()["id"]
    assert offering["duration_minutes"] == 45
    assert offering["is_active"] is True

    async with db_engine.connect() as conn:
        configs = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM session_type_booking_configs c "
                    "  JOIN session_types st ON st.id = c.session_type_id "
                    " WHERE st.mentor_user_id = :u"
                ),
                {"u": mentor},
            )
        ).scalar_one()
    assert configs == 1


async def test_a_refused_create_leaves_neither_row(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A refused create leaves no offering behind.

    **Weaker than it first looks, and worth saying so.** The reachable refusal —
    the name clash — is raised by the index on the `session_types` insert
    itself, so there is nothing written yet to roll back. This pins the count
    rather than the transaction.

    **No partial write is reachable through this endpoint today**, because the
    write model's bounds match the column `CHECK`s exactly: `duration_minutes`
    is 5 to 480 on both sides, and `min_notice_minutes` is refused at the boundary
    well inside the column's 0 to 43200. So nothing a client can send passes
    Pydantic and then fails the config insert.

    That is a property of today's constraints, not a guarantee. The moment the
    two disagree — a wider column, a narrower boundary, a new config column with
    its own rule — a partial write becomes reachable, and the shared transaction
    in `create_session_type` is what keeps it from leaving an offering with no
    config: invisible to every read and to `/slots`, and unreachable by any
    endpoint that could repair it. This test is where that case belongs when it
    becomes constructible.
    """
    mentor, auth_id = await as_mentor(db_engine, "rollback")
    await add_session_type(db_engine, mentor, name="SOP review")

    refused = await api_client.post(URL, json=body(), headers=bearer(api_token(auth_id)))
    assert refused.status_code == 409, refused.text

    async with db_engine.connect() as conn:
        offerings = (
            await conn.execute(
                text("SELECT count(*) FROM session_types WHERE mentor_user_id = :u"),
                {"u": mentor},
            )
        ).scalar_one()
    assert offerings == 1, "the refused insert left an offering behind, with no config"


async def test_a_deleted_name_does_not_reserve_itself(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The index is partial on `deleted_at IS NULL`, and the message says so.

    A mentor who deleted "SOP review" and is told the name is taken would have
    no way to find the row holding it.
    """
    mentor, auth_id = await as_mentor(db_engine, "reuse-name")
    await add_session_type(db_engine, mentor, name="SOP review", deleted=True)

    created = await api_client.post(URL, json=body(), headers=bearer(api_token(auth_id)))

    assert created.status_code == 201, created.text


async def test_a_non_mentor_gets_404_rather_than_a_constraint_error(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`session_types.mentor_user_id` references `mentor_profiles`, so the insert
    would raise a foreign-key violation — a 500 describing a constraint, for a
    request that is simply not this caller's to make.

    The read endpoint answers a non-mentor with an empty page, and that reasoning
    does not carry: there is no true empty answer to a write.
    """
    auth_id = await as_mentee(db_engine, "not-a-mentor")

    refused = await api_client.post(URL, json=body(), headers=bearer(api_token(auth_id)))

    assert refused.status_code == 404, refused.text


@pytest.mark.parametrize("notice", [0, 60, 1439, 4321])
async def test_notice_outside_the_product_range_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, notice: int
) -> None:
    """**The boundary carries the product rule; the column carries sanity.**

    24 hours is the platform floor — no same-day booking — and 72 the current
    ceiling. The column's `CHECK` is `BETWEEN 0 AND 43200` and stays that way:
    a database refuses what is *impossible*, an application refuses what is
    *disallowed*, and when `booking_policies` lands this range moves there as a
    config change rather than a migration (settled decision #104).

    `0` is in the list because it is what 54 test fixtures pass, so it is the
    value most likely to reach this endpoint by accident.
    """
    _, auth_id = await as_mentor(db_engine, f"notice-{notice}")

    refused = await api_client.post(
        URL, json=body(min_notice_minutes=notice), headers=bearer(api_token(auth_id))
    )

    assert refused.status_code == 422, refused.text


async def test_omitting_notice_takes_the_platform_floor(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Sending nothing is not sending zero."""
    _, auth_id = await as_mentor(db_engine, "notice-default")

    await api_client.post(URL, json=body(), headers=bearer(api_token(auth_id)))

    listed = await api_client.get(URL, headers=bearer(api_token(auth_id)))
    (offering,) = listed.json()["data"]
    assert offering["min_notice_minutes"] == 1440


async def test_a_new_offering_resolves_a_venue_without_choosing_one(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`meeting_venue` is not writable, and the response still carries one.

    A new offering leaves `conferencing_option_id` null — *use my default* —
    which resolves to the mentor's default and then to the platform fallback. The
    field is required on the way out, so "not writable" must not mean "absent".
    """
    _, auth_id = await as_mentor(db_engine, "venue-resolves")

    await api_client.post(URL, json=body(), headers=bearer(api_token(auth_id)))

    listed = await api_client.get(URL, headers=bearer(api_token(auth_id)))
    (offering,) = listed.json()["data"]
    assert offering["meeting_venue"] == "google_meet"


# --------------------------------------------------------------------------
# Change
# --------------------------------------------------------------------------


async def test_switching_an_offering_off_is_refused_by_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The 409 that PR 13 removed the need for.**

    While `trg_refuse_retiring_a_primary_offering` existed, this toggle fired it
    — the trigger ran on `UPDATE` as well as delete — so it needed the same `409`
    mapping as `DELETE` or it returned a 500. The trigger went with the pointer,
    so the toggle is plain, and the offering stays in the owner's list flagged
    rather than disappearing.
    """
    mentor, auth_id = await as_mentor(db_engine, "toggle-off")
    session_type = await add_session_type(db_engine, mentor, name="SOP review")

    changed = await api_client.patch(
        f"{URL}/{session_type}", json={"is_active": False}, headers=bearer(api_token(auth_id))
    )
    assert changed.status_code == 200, changed.text

    listed = await api_client.get(URL, headers=bearer(api_token(auth_id)))
    (offering,) = listed.json()["data"]
    assert offering["is_active"] is False


async def test_a_switched_off_offering_is_still_editable(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The reason the scope is `session_type_of()` and not the live predicate.

    Scoping a write on `is_active` would make switching one back on impossible —
    the one edit a paused offering exists to receive.
    """
    mentor, auth_id = await as_mentor(db_engine, "edit-paused")
    session_type = await add_session_type(db_engine, mentor, name="Paused", active=False)

    changed = await api_client.patch(
        f"{URL}/{session_type}", json={"is_active": True}, headers=bearer(api_token(auth_id))
    )

    assert changed.status_code == 200, changed.text


async def test_a_patch_touches_only_what_it_names(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Absent is not null, and the two tables are updated independently.

    Without `exclude_unset` every omitted field is written as its default, so a
    rename blanks the description and resets duration — and the two-table split
    means half of that would land in each.
    """
    mentor, auth_id = await as_mentor(db_engine, "partial-patch")
    session_type = await add_session_type(
        db_engine, mentor, name="Before", description="kept", duration=90, notice=1440
    )

    await api_client.patch(
        f"{URL}/{session_type}", json={"name": "After"}, headers=bearer(api_token(auth_id))
    )

    listed = await api_client.get(URL, headers=bearer(api_token(auth_id)))
    (offering,) = listed.json()["data"]
    assert offering["name"] == "After"
    assert offering["description"] == "kept"
    assert offering["duration_minutes"] == 90
    assert offering["min_notice_minutes"] == 1440


async def test_another_mentors_offering_is_not_found(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Scoped in the query, so the row is never fetched — not fetched and refused.

    `404` rather than `403`: the latter confirms the id exists.
    """
    _, auth_id = await as_mentor(db_engine, "patch-mine")
    other = await make_public_mentor(db_engine, "patch-theirs")
    theirs = await add_session_type(db_engine, other, name="Theirs")

    refused = await api_client.patch(
        f"{URL}/{theirs}", json={"name": "Mine now"}, headers=bearer(api_token(auth_id))
    )

    assert refused.status_code == 404, refused.text

    async with db_engine.connect() as conn:
        name = (
            await conn.execute(text("SELECT name FROM session_types WHERE id = :t"), {"t": theirs})
        ).scalar_one()
    assert name == "Theirs", "the refusal answered 404 and wrote anyway"


async def test_a_deleted_offering_is_not_found(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Soft deletion is the other half of `session_type_of()`. `is_active` is
    reversible through this endpoint; deletion is not."""
    mentor, auth_id = await as_mentor(db_engine, "patch-deleted")
    gone = await add_session_type(db_engine, mentor, name="Gone", deleted=True)

    refused = await api_client.patch(
        f"{URL}/{gone}", json={"name": "Back"}, headers=bearer(api_token(auth_id))
    )

    assert refused.status_code == 404, refused.text


async def test_renaming_onto_a_live_name_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The same index guards the rename, and a create-only mapping would miss it."""
    mentor, auth_id = await as_mentor(db_engine, "rename-clash")
    await add_session_type(db_engine, mentor, name="Taken")
    mine = await add_session_type(db_engine, mentor, name="Mine")

    refused = await api_client.patch(
        f"{URL}/{mine}", json={"name": "Taken"}, headers=bearer(api_token(auth_id))
    )

    assert refused.status_code == 409, refused.text
