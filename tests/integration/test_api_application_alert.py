"""Telling the admins that somebody applied to be a mentor.

**The first message here whose recipients are a set.** Every other one resolves
a person from a row — the mentor on this session, the applicant on this profile.
This one asks *who currently holds a grant that can act*, which is a different
question and the reason it does not go through `AUDIENCE`.

Without it an application is found by somebody thinking to look, which is how a
queue grows quietly.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

APPLY = "/api/v1/users/{user_id}/mentor-profile"


async def a_user(engine: AsyncEngine, tag: str, *, role: str = "mentee") -> tuple[UUID, UUID]:
    auth_id = uuid4()
    async with engine.begin() as conn:
        user_id = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
                    "VALUES (:e, :a, 'Ada', :r, 'Africa/Lagos') RETURNING id"
                ),
                {"e": f"{tag}@example.test", "a": auth_id, "r": role},
            )
        ).scalar_one()
    return user_id, auth_id


async def grant(engine: AsyncEngine, user_id: UUID, role: str, *, revoked: bool = False) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO admin_users (user_id, admin_role) VALUES (:u, :r)"),
            {"u": user_id, "r": role},
        )
        if revoked:
            await conn.execute(
                text(
                    "UPDATE admin_users SET revoked_at = now() "
                    "WHERE user_id = :u AND admin_role = :r"
                ),
                {"u": user_id, "r": role},
            )


async def alerted(engine: AsyncEngine, applicant: UUID) -> set[UUID]:
    async with engine.begin() as conn:
        return {
            UUID(str(row["payload"]["recipient_id"]))
            for row in (
                await conn.execute(
                    text(
                        "SELECT payload FROM outbox_events "
                        "WHERE event_type = 'mentor_application_received' AND entity_id = :i"
                    ),
                    {"i": applicant},
                )
            ).mappings()
        }


async def apply_as(client: httpx.AsyncClient, user_id: UUID, auth_id: UUID) -> httpx.Response:
    return await client.post(
        APPLY.format(user_id=user_id),
        headers=bearer(api_token(auth_id)),
        json={"headline": "I help with applications"},
    )


# --------------------------------------------------------------------------
# Who is told
# --------------------------------------------------------------------------


async def test_the_admins_who_can_act_are_told(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    approver, _ = await a_user(db_engine, "alert-approver")
    await grant(db_engine, approver, "mentor_approval")
    superuser, _ = await a_user(db_engine, "alert-super")
    await grant(db_engine, superuser, "super_admin")
    applicant, auth_id = await a_user(db_engine, "alert-applicant")

    response = await apply_as(api_client, applicant, auth_id)

    assert response.status_code == 201, response.text
    assert await alerted(db_engine, applicant) == {approver, superuser}


async def test_an_admin_who_may_only_look_is_not_told(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**`limited_access` may look and not act**, which the admin suite asserts.

    Telling them about a queue they cannot clear is a message whose only
    possible response is to find somebody else.
    """
    watcher, _ = await a_user(db_engine, "alert-watcher")
    await grant(db_engine, watcher, "limited_access")
    applicant, auth_id = await a_user(db_engine, "alert-limited")

    await apply_as(api_client, applicant, auth_id)

    assert await alerted(db_engine, applicant) == set()


async def test_a_revoked_admin_is_not_told(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`revoked_at IS NULL` is what makes revocation mean anything. An alert
    reaching a revoked admin would be the first place that stopped being true."""
    former, _ = await a_user(db_engine, "alert-former")
    await grant(db_engine, former, "mentor_approval", revoked=True)
    applicant, auth_id = await a_user(db_engine, "alert-revoked")

    await apply_as(api_client, applicant, auth_id)

    assert await alerted(db_engine, applicant) == set()


async def test_an_applicant_who_is_also_an_admin_is_not_told_about_themselves(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Being told to review your own application is noise.

    That the decision endpoint *permits* deciding your own — visible rather than
    prevented, because on a small team blocking it means the only admin can
    never be approved — is a different question from being nudged about it.
    """
    applicant, auth_id = await a_user(db_engine, "alert-self")
    await grant(db_engine, applicant, "super_admin")

    await apply_as(api_client, applicant, auth_id)

    assert applicant not in await alerted(db_engine, applicant)


async def test_one_row_per_admin(api_client: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    """The outbox stores messages that way so a send failing for one person is
    retried for that one."""
    for tag in ("a", "b", "c"):
        admin, _ = await a_user(db_engine, f"alert-many-{tag}")
        await grant(db_engine, admin, "mentor_approval")
    applicant, auth_id = await a_user(db_engine, "alert-many-applicant")

    await apply_as(api_client, applicant, auth_id)

    async with db_engine.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM outbox_events "
                    "WHERE event_type = 'mentor_application_received' AND entity_id = :i"
                ),
                {"i": applicant},
            )
        ).scalar_one()
    assert rows == 3


# --------------------------------------------------------------------------
# When nobody can be told
# --------------------------------------------------------------------------


async def test_an_application_succeeds_with_no_admins_configured(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The wrong way round would be refusing it.** A deployment with no admins
    yet still accepts applications; they simply wait to be found."""
    applicant, auth_id = await a_user(db_engine, "alert-none")

    response = await apply_as(api_client, applicant, auth_id)

    assert response.status_code == 201, response.text
    assert await alerted(db_engine, applicant) == set()


async def test_the_alert_and_the_profile_are_one_transaction(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """No alert about a row that failed to write, and no row nobody was told
    about. Asserted by their both being present after one request."""
    approver, _ = await a_user(db_engine, "alert-atomic-admin")
    await grant(db_engine, approver, "mentor_approval")
    applicant, auth_id = await a_user(db_engine, "alert-atomic")

    await apply_as(api_client, applicant, auth_id)

    async with db_engine.begin() as conn:
        profiles = (
            await conn.execute(
                text("SELECT count(*) FROM mentor_profiles WHERE user_id = :u"), {"u": applicant}
            )
        ).scalar_one()
    assert profiles == 1
    assert await alerted(db_engine, applicant) == {approver}


# --------------------------------------------------------------------------
# The message itself
# --------------------------------------------------------------------------


async def test_it_is_about_the_application_not_a_session(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`entity_type` decides what context the drain loads. A message about a
    profile has no session, so a template asking for `sessionDate` is a template
    pointed at the wrong message — refused by name rather than sent blank."""
    approver, _ = await a_user(db_engine, "alert-entity-admin")
    await grant(db_engine, approver, "mentor_approval")
    applicant, auth_id = await a_user(db_engine, "alert-entity")

    await apply_as(api_client, applicant, auth_id)

    async with db_engine.begin() as conn:
        row: Any = (
            (
                await conn.execute(
                    text(
                        "SELECT entity_type, entity_id FROM outbox_events "
                        "WHERE event_type = 'mentor_application_received' AND entity_id = :i"
                    ),
                    {"i": applicant},
                )
            )
            .mappings()
            .one()
        )
    assert row["entity_type"] == "mentor_profile"
    assert UUID(str(row["entity_id"])) == applicant
