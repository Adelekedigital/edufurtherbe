"""FastAPI dependency wiring.

One of the two sanctioned composition points (the other is ``main.py``), and
exempt from the layer check for that reason — this is where concrete ``infra``
classes get bound to what the routes ask for.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.errors import AuthenticationError, NotFoundError
from app.infra.auth.supabase import SupabaseTokenVerifier, TokenClaims
from app.infra.db.engine import create_database_engine, create_session_factory

SettingsDep = Annotated[Settings, Depends(get_settings)]

# `auto_error=False` so a missing header reaches our handler rather than
# FastAPI's, which would answer in its own `{"detail": ...}` shape and break the
# promise that every failure is Problem Details.
bearer = HTTPBearer(auto_error=False)


@lru_cache(maxsize=1)
def get_verifier() -> SupabaseTokenVerifier:
    """One verifier per process; it caches the Supabase key set."""
    settings = get_settings()
    return SupabaseTokenVerifier(
        jwks_url=settings.supabase_jwks_url,
        secret=(
            settings.supabase_jwt_secret.get_secret_value()
            if settings.supabase_jwt_secret
            else None
        ),
    )


@lru_cache(maxsize=1)
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """The engine and session factory, built once.

    An engine per request would open a connection pool per request — the kind of
    thing that works in development and exhausts the database under any load.
    """
    return create_session_factory(create_database_engine(get_settings()))


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """A session per request, closed afterwards.

    Taken off ``app.state`` when the application put one there, which is what
    lets a test bind a factory to its own disposable database without touching
    the process-wide cache.
    """
    factory = getattr(request.app.state, "session_factory", None) or get_session_factory()
    async with factory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_claims(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> TokenClaims:
    """Verify the bearer token, or refuse.

    A missing header and a bad token raise the *same* error. Separating them is
    a small courtesy to a client and a small gift to anyone probing which tokens
    are shaped right.
    """
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("no bearer token")

    verifier = getattr(request.app.state, "token_verifier", None) or get_verifier()
    return verifier.verify(credentials.credentials)


ClaimsDep = Annotated[TokenClaims, Depends(get_claims)]

# The whole translation, in one statement.
#
# The token's `sub` is a Supabase identifier and `users.id` is ours — two of the
# three identifier spaces tier 2 says must never be interchangeable — so this is
# where one becomes the other, exactly once per request.
#
# `is_admin` is resolved *in the query* rather than fetched and checked after,
# per non-negotiable #5. It is profile existence, not a column: an admin is a
# user holding a live grant, and `revoked_at IS NULL` is what makes revocation
# actually revoke.
CURRENT_USER = text("""
    SELECT u.id, u.email, u.first_name, u.last_name, u.slug, u.primary_role,
           u.timezone, u.email_verified_at, u.created_at,
           p.about_me, p.gender, p.avatar_url, p.banner_url,
           p.social_linkedin, p.social_twitter, p.social_youtube,
           (p.user_id IS NOT NULL) AS has_profile,
           EXISTS (
               SELECT 1 FROM admin_users a
               WHERE a.user_id = u.id AND a.revoked_at IS NULL
           ) AS is_admin
    FROM users u
    LEFT JOIN user_profiles p ON p.user_id = u.id
    WHERE u.auth_id = :auth_id AND u.deleted_at IS NULL
""")


async def get_current_user(claims: ClaimsDep, session: SessionDep) -> dict[str, Any]:
    """Resolve a verified token to the user it belongs to.

    **A valid token for a user we do not hold is a 404, not a 401.** The token is
    genuine; no account is linked to it. Every migrated user is in exactly that
    state until provisioning runs, so during cutover this is the ordinary case
    rather than an attack — and ``NotFoundError`` already conflates "absent" with
    "not yours", which is the right answer either way.
    """
    result = await session.execute(CURRENT_USER, {"auth_id": claims.subject})
    row = result.mappings().first()
    if row is None:
        raise NotFoundError("no account is linked to this identity")
    return dict(row)


CurrentUserDep = Annotated[dict[str, Any], Depends(get_current_user)]
