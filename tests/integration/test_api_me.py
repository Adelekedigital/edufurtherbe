"""``GET /api/me``, and the authentication that guards it.

**The tests that matter here assert rejection.** An auth check that accepts a
valid token proves very little; one that accepts an unsigned or wrongly-signed
token is worse than no check at all, because it reports success while admitting
everyone. So there is one acceptance case and six refusals.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from conftest import PROBLEM_JSON, api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

# Only the *wrong* key stays local: it exists to prove a signature check, and
# nothing else needs it. `SECRET`, `api_token`, `bearer` and the `api_client`
# fixture moved to `conftest.py` when a second API test file needed them.
OTHER_SECRET = "not-the-signing-secret"  # noqa: S105


async def seed_user(engine: AsyncEngine, auth_id: UUID, *, admin: bool = False) -> None:
    async with engine.begin() as conn:
        row = await conn.execute(
            text(
                "INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
                "VALUES ('someone@example.com', :a, 'Ada', 'mentor', 'Africa/Lagos') "
                "RETURNING id"
            ),
            {"a": auth_id},
        )
        user_id = row.scalar_one()
        await conn.execute(
            text("INSERT INTO user_profiles (user_id, about_me) VALUES (:u, 'Hello')"),
            {"u": user_id},
        )
        if admin:
            await conn.execute(
                text("INSERT INTO admin_users (user_id, admin_role) VALUES (:u, 'super_admin')"),
                {"u": user_id},
            )


# --------------------------------------------------------------------------
# refusals — the reason this file exists
# --------------------------------------------------------------------------


async def test_no_token_is_refused(api_client: httpx.AsyncClient) -> None:
    response = await api_client.get("/api/v1/me")

    assert response.status_code == 401
    assert response.headers["content-type"].startswith(PROBLEM_JSON)


async def test_an_unsigned_token_is_refused(api_client: httpx.AsyncClient) -> None:
    """``alg: none`` is the oldest JWT attack there is, and it works against any
    verifier that trusts the header's algorithm instead of naming its own."""
    unsigned = jwt.encode({"sub": str(uuid4()), "aud": "authenticated"}, key="", algorithm="none")

    assert (await api_client.get("/api/v1/me", headers=bearer(unsigned))).status_code == 401


async def test_a_token_signed_with_the_wrong_key_is_refused(api_client: httpx.AsyncClient) -> None:
    assert (
        await api_client.get("/api/v1/me", headers=bearer(api_token(uuid4(), secret=OTHER_SECRET)))
    ).status_code == 401


async def test_an_expired_token_is_refused(api_client: httpx.AsyncClient) -> None:
    stale = api_token(uuid4(), exp=datetime.now(UTC) - timedelta(seconds=1))

    assert (await api_client.get("/api/v1/me", headers=bearer(stale))).status_code == 401


async def test_a_token_for_another_audience_is_refused(api_client: httpx.AsyncClient) -> None:
    """Supabase issues ``aud: authenticated``. A token minted for a different
    audience is a token for a different system, however genuine its signature."""
    elsewhere = api_token(uuid4(), aud="some-other-service")

    assert (await api_client.get("/api/v1/me", headers=bearer(elsewhere))).status_code == 401


async def test_a_subject_that_is_not_a_uuid_is_refused(api_client: httpx.AsyncClient) -> None:
    """It could never match ``users.auth_id``. Letting it through would turn an
    authentication failure into a database error somewhere less obvious."""
    assert (
        await api_client.get("/api/v1/me", headers=bearer(api_token("not-a-uuid")))
    ).status_code == 401


async def test_every_refusal_reads_the_same(api_client: httpx.AsyncClient) -> None:
    """Telling a caller *why* their token failed tells anyone probing which half
    of the guess was right."""
    bodies = [
        (
            await api_client.get(
                "/api/v1/me", headers=bearer(api_token(uuid4(), secret=OTHER_SECRET))
            )
        ).json(),
        (await api_client.get("/api/v1/me", headers=bearer("not.a.token"))).json(),
        (await api_client.get("/api/v1/me")).json(),
    ]

    assert len({body.get("detail") for body in bodies}) == 1, bodies
    assert len({body["status"] for body in bodies}) == 1


# --------------------------------------------------------------------------
# acceptance
# --------------------------------------------------------------------------


async def test_a_valid_token_with_no_linked_account_is_a_404(api_client: httpx.AsyncClient) -> None:
    """The state every migrated user is in until provisioning runs.

    The token is genuine; nothing is linked to it. That is not an authentication
    failure, and reporting it as one would send an operator looking at the wrong
    thing during cutover.
    """
    response = await api_client.get("/api/v1/me", headers=bearer(api_token(uuid4())))

    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_JSON)


async def test_the_signed_in_user_is_returned(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    auth_id = uuid4()
    await seed_user(db_engine, auth_id)

    body = (await api_client.get("/api/v1/me", headers=bearer(api_token(auth_id)))).json()

    assert body["email"] == "someone@example.com"
    assert body["first_name"] == "Ada"
    assert body["primary_role"] == "mentor"
    assert body["profile"]["about_me"] == "Hello"
    assert body["is_admin"] is False


async def test_the_vendor_and_migration_identifiers_are_never_returned(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """One is Supabase's identifier and the other a Bubble anchor. Neither is
    anybody's business outside this service, and a client that learned either
    would start depending on it."""
    auth_id = uuid4()
    await seed_user(db_engine, auth_id)

    body = (await api_client.get("/api/v1/me", headers=bearer(api_token(auth_id)))).json()

    assert "auth_id" not in body
    assert "legacy_bubble_id" not in body


async def test_is_admin_reflects_a_live_grant(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Authorization is grant existence, not a column — and ``revoked_at IS NULL``
    is what makes revocation actually revoke."""
    auth_id = uuid4()
    await seed_user(db_engine, auth_id, admin=True)

    assert (await api_client.get("/api/v1/me", headers=bearer(api_token(auth_id)))).json()[
        "is_admin"
    ] is True

    async with db_engine.begin() as conn:
        await conn.execute(text("UPDATE admin_users SET revoked_at = now()"))

    assert (await api_client.get("/api/v1/me", headers=bearer(api_token(auth_id)))).json()[
        "is_admin"
    ] is False


async def test_a_soft_deleted_user_cannot_sign_in(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    auth_id = uuid4()
    await seed_user(db_engine, auth_id)

    async with db_engine.begin() as conn:
        await conn.execute(text("UPDATE users SET deleted_at = now()"))

    assert (
        await api_client.get("/api/v1/me", headers=bearer(api_token(auth_id)))
    ).status_code == 404
