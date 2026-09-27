"""A person's first sign-in creates their account (settled decision #178).

Before this, a valid token for someone with no `users` row was a 404 forever:
accounts existed only for migrated users, provisioned ahead of time. Now the
first authenticated request creates the row from the token.

**The guardrails are the tests that matter most.** It never links to or merges
with an existing account by email — that is how an account would be taken over
— it never resurrects a deleted one, and two first requests racing create one
account.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]

URL = "/api/v1/me"


def token_for(auth_id: UUID, email: str | None) -> dict[str, str]:
    return bearer(api_token(auth_id, email=email))


BY_AUTH_ID = text(
    "SELECT id, email, auth_id, email_verified_at, primary_role, deleted_at "
    "FROM users WHERE auth_id = :v"
)
BY_EMAIL = text(
    "SELECT id, email, auth_id, email_verified_at, primary_role, deleted_at "
    "FROM users WHERE email = :v"
)


async def users_with(
    engine: AsyncEngine, *, auth_id: UUID | None = None, email: str | None = None
) -> list[dict[str, object]]:
    """Rows for one sign-in or one address, deleted ones included."""
    query, value = (BY_AUTH_ID, auth_id) if auth_id is not None else (BY_EMAIL, email)
    async with engine.begin() as conn:
        rows = await conn.execute(query, {"v": value})
        return [dict(row._mapping) for row in rows]


async def add_user(
    engine: AsyncEngine, email: str, *, auth_id: UUID | None = None, deleted: bool = False
) -> UUID:
    async with engine.begin() as conn:
        user = await conn.execute(
            text(
                "INSERT INTO users (email, auth_id, primary_role, timezone, deleted_at) "
                "VALUES (:e, :a, 'mentee', 'UTC', CASE WHEN :d THEN now() END) RETURNING id"
            ),
            {"e": email, "a": auth_id, "d": deleted},
        )
        return UUID(str(user.scalar_one()))


async def test_a_first_sign_in_creates_the_account(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()

    response = await api_client.get(URL, headers=token_for(auth_id, "new.person@example.com"))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["email"] == "new.person@example.com"
    assert body["primary_role"] == "mentee"
    assert body["goal"] is None
    assert body["mentor_profile"] is None
    (row,) = await users_with(db_engine, auth_id=auth_id)
    assert row["email_verified_at"] is not None


async def test_signing_in_again_does_not_create_a_second_account(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    headers = token_for(auth_id, "twice@example.com")

    first = (await api_client.get(URL, headers=headers)).json()
    again = (await api_client.get(URL, headers=headers)).json()

    assert first["id"] == again["id"]
    assert len(await users_with(db_engine, email="twice@example.com")) == 1


async def test_two_first_requests_create_one_account(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    headers = token_for(auth_id, "racing@example.com")

    first, second = await asyncio.gather(
        api_client.get(URL, headers=headers), api_client.get(URL, headers=headers)
    )

    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert len(await users_with(db_engine, auth_id=auth_id)) == 1


async def test_the_email_is_stored_lowercase(api_client: httpx.AsyncClient) -> None:
    auth_id = uuid4()

    response = await api_client.get(URL, headers=token_for(auth_id, "Mixed.Case@Example.com"))

    assert response.status_code == 200, response.text
    assert response.json()["email"] == "mixed.case@example.com"


async def test_an_existing_account_is_used_not_duplicated(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A migrated user was provisioned with this sign-in already."""
    auth_id = uuid4()
    existing = await add_user(db_engine, "migrated@example.com", auth_id=auth_id)

    response = await api_client.get(URL, headers=token_for(auth_id, "migrated@example.com"))

    assert response.json()["id"] == str(existing)
    assert len(await users_with(db_engine, email="migrated@example.com")) == 1


# --------------------------------------------------------------------------
# Never linked, never merged, never resurrected
# --------------------------------------------------------------------------


async def test_an_unlinked_account_with_the_same_email_is_not_taken_over(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Provisioning missed this migrated user. A new sign-in with their email
    must not be handed their account — that is the takeover this rule exists
    to prevent — and must not create a second account beside it."""
    unlinked = await add_user(db_engine, "missed@example.com", auth_id=None)

    response = await api_client.get(URL, headers=token_for(uuid4(), "missed@example.com"))

    assert response.status_code == 409
    assert response.json()["type"] == "/problems/account-exists"
    (row,) = await users_with(db_engine, email="missed@example.com")
    assert row["id"] == unlinked
    assert row["auth_id"] is None


async def test_an_email_linked_to_another_sign_in_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    await add_user(db_engine, "taken@example.com", auth_id=uuid4())

    response = await api_client.get(URL, headers=token_for(uuid4(), "taken@example.com"))

    assert response.status_code == 409
    assert response.json()["type"] == "/problems/account-exists"


async def test_a_deleted_account_is_not_brought_back(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    await add_user(db_engine, "gone@example.com", auth_id=auth_id, deleted=True)

    response = await api_client.get(URL, headers=token_for(auth_id, "gone@example.com"))

    assert response.status_code == 404
    assert len(await users_with(db_engine, auth_id=auth_id)) == 1


async def test_a_token_with_no_email_creates_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Anonymous and phone-only sign-ins carry no email; there is nothing to
    build an account from."""
    auth_id = uuid4()

    response = await api_client.get(URL, headers=token_for(auth_id, None))

    assert response.status_code == 404
    assert await users_with(db_engine, auth_id=auth_id) == []
