"""``GET /api/me``, and the authentication that guards it.

**The tests that matter here assert rejection.** An auth check that accepts a
valid token proves very little; one that accepts an unsigned or wrongly-signed
token is worse than no check at all, because it reports success while admitting
everyone. So there is one acceptance case and six refusals.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.infra.auth.supabase import SupabaseTokenVerifier
from app.infra.db.engine import create_session_factory
from app.main import create_app

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

# Local signing keys, used only to mint tokens for this file. Flagged by S105 as
# hardcoded credentials, which is exactly what they are — a test that verified
# signatures against a real key would be testing the key, not the verifier.
SECRET = "test-signing-secret"  # noqa: S105
OTHER_SECRET = "not-the-signing-secret"  # noqa: S105
PROBLEM_JSON = "application/problem+json"
AUTH = "Authorization"


def token(subject: str | UUID, *, secret: str = SECRET, **overrides: Any) -> str:
    claims: dict[str, Any] = {
        "sub": str(subject),
        "aud": "authenticated",
        "exp": datetime.now(UTC) + timedelta(hours=1),
        "email": "someone@example.com",
    }
    return jwt.encode(claims | overrides, secret, algorithm="HS256")


def header(value: str) -> dict[str, str]:
    return {AUTH: f"Bearer {value}"}


@pytest_asyncio.fixture
async def client(db_engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    """An app bound to this test's disposable database and a local signing key.

    Both are injected through ``app.state`` rather than the process-wide caches,
    so nothing here mutates state another test would inherit.

    ``httpx.AsyncClient`` over ASGI rather than ``TestClient``: the sync client
    drives the app on its own event loop, while ``db_engine`` is bound to the one
    pytest-asyncio is running. asyncpg notices, and the failure reads
    "attached to a different loop" — which sounds like an asyncpg bug and is a
    test-harness one.
    """
    app = create_app(Settings(_env_file=None))
    app.state.session_factory = create_session_factory(db_engine)
    app.state.token_verifier = SupabaseTokenVerifier(secret=SECRET)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


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


async def test_no_token_is_refused(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/me")

    assert response.status_code == 401
    assert response.headers["content-type"].startswith(PROBLEM_JSON)


async def test_an_unsigned_token_is_refused(client: httpx.AsyncClient) -> None:
    """``alg: none`` is the oldest JWT attack there is, and it works against any
    verifier that trusts the header's algorithm instead of naming its own."""
    unsigned = jwt.encode({"sub": str(uuid4()), "aud": "authenticated"}, key="", algorithm="none")

    assert (await client.get("/api/me", headers=header(unsigned))).status_code == 401


async def test_a_token_signed_with_the_wrong_key_is_refused(client: httpx.AsyncClient) -> None:
    assert (
        await client.get("/api/me", headers=header(token(uuid4(), secret=OTHER_SECRET)))
    ).status_code == 401


async def test_an_expired_token_is_refused(client: httpx.AsyncClient) -> None:
    stale = token(uuid4(), exp=datetime.now(UTC) - timedelta(seconds=1))

    assert (await client.get("/api/me", headers=header(stale))).status_code == 401


async def test_a_token_for_another_audience_is_refused(client: httpx.AsyncClient) -> None:
    """Supabase issues ``aud: authenticated``. A token minted for a different
    audience is a token for a different system, however genuine its signature."""
    elsewhere = token(uuid4(), aud="some-other-service")

    assert (await client.get("/api/me", headers=header(elsewhere))).status_code == 401


async def test_a_subject_that_is_not_a_uuid_is_refused(client: httpx.AsyncClient) -> None:
    """It could never match ``users.auth_id``. Letting it through would turn an
    authentication failure into a database error somewhere less obvious."""
    assert (await client.get("/api/me", headers=header(token("not-a-uuid")))).status_code == 401


async def test_every_refusal_reads_the_same(client: httpx.AsyncClient) -> None:
    """Telling a caller *why* their token failed tells anyone probing which half
    of the guess was right."""
    bodies = [
        (await client.get("/api/me", headers=header(token(uuid4(), secret=OTHER_SECRET)))).json(),
        (await client.get("/api/me", headers=header("not.a.token"))).json(),
        (await client.get("/api/me")).json(),
    ]

    assert len({body.get("detail") for body in bodies}) == 1, bodies
    assert len({body["status"] for body in bodies}) == 1


# --------------------------------------------------------------------------
# acceptance
# --------------------------------------------------------------------------


async def test_a_valid_token_with_no_linked_account_is_a_404(client: httpx.AsyncClient) -> None:
    """The state every migrated user is in until provisioning runs.

    The token is genuine; nothing is linked to it. That is not an authentication
    failure, and reporting it as one would send an operator looking at the wrong
    thing during cutover.
    """
    response = await client.get("/api/me", headers=header(token(uuid4())))

    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_JSON)


async def test_the_signed_in_user_is_returned(
    db_engine: AsyncEngine, client: httpx.AsyncClient
) -> None:
    auth_id = uuid4()
    await seed_user(db_engine, auth_id)

    body = (await client.get("/api/me", headers=header(token(auth_id)))).json()

    assert body["email"] == "someone@example.com"
    assert body["first_name"] == "Ada"
    assert body["primary_role"] == "mentor"
    assert body["profile"]["about_me"] == "Hello"
    assert body["is_admin"] is False


async def test_the_vendor_and_migration_identifiers_are_never_returned(
    db_engine: AsyncEngine, client: httpx.AsyncClient
) -> None:
    """One is Supabase's identifier and the other a Bubble anchor. Neither is
    anybody's business outside this service, and a client that learned either
    would start depending on it."""
    auth_id = uuid4()
    await seed_user(db_engine, auth_id)

    body = (await client.get("/api/me", headers=header(token(auth_id)))).json()

    assert "auth_id" not in body
    assert "legacy_bubble_id" not in body


async def test_is_admin_reflects_a_live_grant(
    db_engine: AsyncEngine, client: httpx.AsyncClient
) -> None:
    """Authorization is grant existence, not a column — and ``revoked_at IS NULL``
    is what makes revocation actually revoke."""
    auth_id = uuid4()
    await seed_user(db_engine, auth_id, admin=True)

    assert (await client.get("/api/me", headers=header(token(auth_id)))).json()["is_admin"] is True

    async with db_engine.begin() as conn:
        await conn.execute(text("UPDATE admin_users SET revoked_at = now()"))

    assert (await client.get("/api/me", headers=header(token(auth_id)))).json()["is_admin"] is False


async def test_a_soft_deleted_user_cannot_sign_in(
    db_engine: AsyncEngine, client: httpx.AsyncClient
) -> None:
    auth_id = uuid4()
    await seed_user(db_engine, auth_id)

    async with db_engine.begin() as conn:
        await conn.execute(text("UPDATE users SET deleted_at = now()"))

    assert (await client.get("/api/me", headers=header(token(auth_id)))).status_code == 404
