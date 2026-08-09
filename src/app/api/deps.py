"""FastAPI dependency wiring.

One of the two sanctioned composition points (the other is ``main.py``), and
exempt from the layer check for that reason — this is where concrete ``infra``
classes get bound to what the routes ask for.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated, Any

from fastapi import Depends, Path, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.schemas.common import (
    LOOKUP_PAGE_SIZE,
    MAX_PAGE_SIZE,
    clamp_limit,
    decode_cursor,
)
from app.core.config import Settings, get_settings
from app.core.errors import AuthenticationError, NotFoundError
from app.infra.auth.supabase import SupabaseTokenVerifier, TokenClaims
from app.infra.db.catalogue_store import LOOKUPS, list_lookup, search_institutions
from app.infra.db.engine import create_database_engine, create_session_factory
from app.infra.db.profile_store import (
    get_mentor_profile,
    list_awards,
    list_education,
    list_goals,
)

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


# Resolving `{user_id}` to a user the caller may actually read.
#
# **The scope is the WHERE clause, not a branch after the fetch** (non-negotiable
# #5). Fetching the target and then testing ownership in Python is the shape that
# reads correctly and leaks anyway: it works until one path forgets the branch,
# and the only difference between a correct and a leaking endpoint is a line
# nothing enforces. Here there is no row to forget about — a caller who may not
# read this user gets nothing back, and the reason is the same statement that
# found them.
#
# `deleted_at IS NULL` is in the same statement for the same reason. A
# soft-deleted user is invisible, and this project has already shipped that rule
# hand-typed into five places with the fifth missed.
TARGET_USER = text("""
    SELECT u.id
    FROM users u
    WHERE u.id = :target
      AND u.deleted_at IS NULL
      AND (u.id = :caller OR :caller_is_admin)
""")


async def get_target_user(
    user_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> uuid.UUID:
    """The user whose records the caller is asking for, if they may have them.

    **A caller who may not read this user gets 404, not 403.** The distinction
    403 would draw — "this exists but is not yours" — is exactly the fact worth
    withholding, and `NotFoundError` already conflates absent with not-yours,
    which is the right answer either way. It also means a wrong id and someone
    else's id are indistinguishable from outside, so the endpoint cannot be used
    to enumerate accounts.

    `is_admin` comes from the live `admin_users` grant `get_current_user`
    resolved — a grant with `revoked_at` set is not an admin. It is never
    `primary_role`, which decides a dashboard and is not an authorization claim.
    """
    result = await session.execute(
        TARGET_USER,
        {"target": user_id, "caller": user["id"], "caller_is_admin": user["is_admin"]},
    )
    row = result.first()
    if row is None:
        raise NotFoundError("no such user")
    return user_id


TargetUserDep = Annotated[uuid.UUID, Depends(get_target_user)]


# --------------------------------------------------------------------------
# Reads, bound here rather than in the routes
#
# `api/` may not import `infra/` — non-negotiable #1, enforced by
# `check_layers.py`. This module is one of the two sanctioned exceptions, and
# that is not a loophole for the routes to reach through: what follows are
# **dependencies that return plain data**, so a route module imports only its
# schemas and these names, and never learns that a store exists.
#
# The alternative shapes were both worse. Re-exporting the store functions from
# here would satisfy the checker while changing nothing real. Adding
# `api/routes/` to the exempt list would weaken the config to pass, which the
# checker's own error message forbids.
# --------------------------------------------------------------------------


async def institution_results(
    session: SessionDep,
    q: Annotated[str, Query(description="What the user has typed so far.")] = "",
    limit: Annotated[int | None, Query(ge=1, le=MAX_PAGE_SIZE)] = None,
) -> list[dict[str, Any]]:
    """Institutions matching ``q``. Declares its own query parameters, so they
    still appear in the OpenAPI schema exactly as if the route named them."""
    return await search_institutions(session, q=q, limit=clamp_limit(limit))


InstitutionResultsDep = Annotated[list[dict[str, Any]], Depends(institution_results)]


async def lookup_page(
    session: SessionDep,
    catalogue: Annotated[str, Path(description="Which catalogue to list.")],
    q: Annotated[str | None, Query(description="Filter by display name.")] = None,
    limit: Annotated[int | None, Query(ge=1, le=LOOKUP_PAGE_SIZE)] = None,
    cursor: Annotated[str | None, Query(description="From a previous `next_cursor`.")] = None,
) -> tuple[list[dict[str, Any]], bool]:
    """One page of a lookup catalogue, and whether another follows."""
    if catalogue not in LOOKUPS:
        # 404 rather than 422: `/catalog/nonsense` is a URL that does not exist.
        raise NotFoundError(f"no catalogue named {catalogue!r}")
    return await list_lookup(
        session,
        catalogue,
        q=q,
        # A bigger default than the shared one: this serves select boxes, and
        # `countries` is 249 rows a client wants in a single call.
        limit=min(limit or LOOKUP_PAGE_SIZE, LOOKUP_PAGE_SIZE),
        cursor=decode_cursor(cursor),
    )


LookupPageDep = Annotated[tuple[list[dict[str, Any]], bool], Depends(lookup_page)]


async def target_education(user_id: TargetUserDep, session: SessionDep) -> list[dict[str, Any]]:
    return await list_education(session, user_id)


async def target_goals(user_id: TargetUserDep, session: SessionDep) -> list[dict[str, Any]]:
    return await list_goals(session, user_id)


async def target_awards(user_id: TargetUserDep, session: SessionDep) -> list[dict[str, Any]]:
    return await list_awards(session, user_id)


async def target_mentor_profile(
    user_id: TargetUserDep, session: SessionDep
) -> dict[str, Any] | None:
    return await get_mentor_profile(session, user_id)


EducationDep = Annotated[list[dict[str, Any]], Depends(target_education)]
GoalsDep = Annotated[list[dict[str, Any]], Depends(target_goals)]
AwardsDep = Annotated[list[dict[str, Any]], Depends(target_awards)]
MentorProfileDep = Annotated[dict[str, Any] | None, Depends(target_mentor_profile)]


async def own_attributes(user: CurrentUserDep, session: SessionDep) -> dict[str, Any]:
    """The caller's own four collections, for the one-call profile render.

    **The same store functions the `/users/{id}/...` dependencies above call.**
    Two queries producing one shape is the duplication non-negotiable #8 names;
    one query used twice is not, and `test_me_and_the_sub_resource_agree` fails
    the moment somebody re-implements either side.

    No authorization argument: `CurrentUserDep` *is* the caller, so there is no
    target to check.
    """
    user_id = user["id"]
    return {
        "education": await list_education(session, user_id),
        "goals": await list_goals(session, user_id),
        "awards": await list_awards(session, user_id),
        "mentor_profile": await get_mentor_profile(session, user_id),
    }


OwnAttributesDep = Annotated[dict[str, Any], Depends(own_attributes)]
