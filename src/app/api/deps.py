"""FastAPI dependency wiring.

One of the two sanctioned composition points (the other is ``main.py``), and
exempt from the layer check for that reason — this is where concrete ``infra``
classes get bound to what the routes ask for.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from functools import lru_cache
from typing import Annotated, Any
from uuid import UUID

import httpx
from fastapi import Depends, File, Path, Query, Request, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from app.api.schemas.admin import DeclineRequest, MergeRequest
from app.api.schemas.availability import (
    AvailabilityExceptionWrite,
    AvailabilityRulePatch,
    AvailabilityRuleWrite,
)
from app.api.schemas.common import (
    LOOKUP_PAGE_SIZE,
    MAX_PAGE_SIZE,
    StorableText,
    clamp_limit,
    decode_cursor,
    decode_id_cursor,
    decode_offset_cursor,
    encode_id_cursor,
    next_offset_cursor,
)
from app.api.schemas.profile import (
    AwardPatch,
    AwardWrite,
    EducationPatch,
    EducationWrite,
    GoalWrite,
    MentorProfileWrite,
    UserLanguagesWrite,
    UserProfileWrite,
)
from app.api.schemas.session_types import MentorSessionTypePatch, MentorSessionTypeWrite
from app.core.config import Settings, get_settings
from app.core.errors import (
    AuthenticationError,
    ConfigurationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.domain.assets import AssetKind, object_path
from app.domain.availability import DEFAULT_PROJECTION_DAYS, UtcInterval
from app.domain.enums import AdminRole
from app.domain.images import MAX_UPLOAD_BYTES, process
from app.infra.auth.supabase import SupabaseTokenVerifier, TokenClaims
from app.infra.db.admin_store import (
    approve_institution,
    merge_institution,
    pending_institutions,
    pending_mentors,
)
from app.infra.db.asset_store import replace_url
from app.infra.db.availability_store import list_exceptions, list_rules
from app.infra.db.availability_writer import (
    create_exception,
    create_rule,
    delete_exception,
    delete_rule,
    update_rule,
)
from app.infra.db.catalogue_store import LOOKUPS, list_lookup, search_institutions
from app.infra.db.education_writer import create_education, delete_education, update_education
from app.infra.db.engine import create_database_engine, create_session_factory
from app.infra.db.mentor_public_store import get_public_mentor
from app.infra.db.mentor_search_store import search_mentors
from app.infra.db.mentor_status_store import (
    decide,
    history,
    may_self_resume,
    pause,
    resume,
    set_listing,
)
from app.infra.db.offerings import offerings_for
from app.infra.db.profile_store import (
    get_goal,
    get_mentor_profile,
    list_awards,
    list_education,
    list_languages,
)
from app.infra.db.profile_writer import (
    create_award,
    create_mentor_profile,
    delete_award,
    delete_goal,
    replace_languages,
    update_award,
    update_mentor_profile,
    upsert_goal,
    upsert_profile,
)
from app.infra.db.session_stats import mentor_stats

# `get_session` is aliased: this module already has one, and it is the **database
# session** dependency at line 142. Two callables with that name in one file is a
# collision a reader resolves by scrolling, and the wrong one is a plausible
# mistake rather than an obvious error — `bubble_id` shadowed a local the same
# way in the M4 transform and raised `UnboundLocalError` far from the edit.
from app.infra.db.session_store import (
    get_session as get_session_row,
)
from app.infra.db.session_store import (
    list_session_events,
    list_sessions,
)
from app.infra.db.session_type_store import (
    create_session_type,
    list_own_session_types,
    list_session_types,
    update_session_type,
)
from app.infra.db.slot_store import list_slots
from app.infra.storage.supabase import StorageError, SupabaseStorage

#: How long a storage call may take before the request gives up.
#:
#: Longer than a database call and much shorter than a browser's patience: the
#: object is at most 5 MB and Supabase is a network hop, so a call still running
#: after this is a call that has failed and not yet said so.
UPLOAD_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

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
# per non-negotiable #5. It is grant existence, not a column: an admin is a user
# holding a live grant, and `revoked_at IS NULL` is what makes revocation
# actually revoke.
#
# **`admin_roles` carries which grants**, because `AdminRole` distinguishes
# `super_admin`, `mentor_approval` and `limited_access` — so "is an admin" is not
# the question an endpoint needs answered. `array_agg` over no rows is `NULL`,
# which is what `is_admin` is derived from: one subquery answering both, rather
# than an `EXISTS` beside an aggregate that could disagree.
CURRENT_USER = text("""
    SELECT u.id, u.email, u.first_name, u.last_name, u.slug, u.primary_role,
           u.timezone, u.email_verified_at, u.created_at,
           p.about_me, p.gender, p.avatar_url, p.banner_url,
           p.social_linkedin, p.social_twitter, p.social_youtube,
           (p.user_id IS NOT NULL) AS has_profile,
           COALESCE(a.roles, ARRAY[]::text[]) AS admin_roles,
           (a.roles IS NOT NULL) AS is_admin
    FROM users u
    LEFT JOIN user_profiles p ON p.user_id = u.id
    LEFT JOIN LATERAL (
        SELECT array_agg(g.admin_role::text) AS roles
        FROM admin_users g
        WHERE g.user_id = u.id AND g.revoked_at IS NULL
    ) a ON TRUE
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
#
# **One statement, two audiences.** Reads permit a live admin; writes do not —
# an admin curates the catalogue, not somebody's education history, and opening
# that later is additive where closing it would not be. Expressing the
# difference as a parameter rather than a second `text()` keeps
# `deleted_at IS NULL` in one place; the alternative is two statements that
# agree until one of them is edited.
TARGET_USER = text("""
    SELECT u.id
    FROM users u
    WHERE u.id = :target
      AND u.deleted_at IS NULL
      AND (u.id = :caller OR (:admin_may AND :caller_is_admin))
""")


async def _resolve_target(
    user_id: uuid.UUID, user: dict[str, Any], session: AsyncSession, *, admin_may: bool
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
        {
            "target": user_id,
            "caller": user["id"],
            "caller_is_admin": user["is_admin"],
            "admin_may": admin_may,
        },
    )
    row = result.first()
    if row is None:
        raise NotFoundError("no such user")
    return user_id


async def get_target_user(
    user_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> uuid.UUID:
    """For reads: the owner, or a live admin."""
    return await _resolve_target(user_id, user, session, admin_may=True)


async def get_owner(user_id: uuid.UUID, user: CurrentUserDep, session: SessionDep) -> uuid.UUID:
    """For writes: the owner, and nobody else.

    **Named differently from `get_target_user` on purpose.** The two differ by
    one clause, and a reader comparing a read route to a write route beside it
    should see the difference in the dependency's name rather than have to open
    this module. An admin reading somebody's education is a review; an admin
    silently editing it is an audit trail nobody has designed.
    """
    return await _resolve_target(user_id, user, session, admin_may=False)


TargetUserDep = Annotated[uuid.UUID, Depends(get_target_user)]
OwnerDep = Annotated[uuid.UUID, Depends(get_owner)]


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
    q: Annotated[str, Query(description="What the user has typed so far."), StorableText] = "",
    limit: Annotated[int | None, Query(ge=1, le=MAX_PAGE_SIZE)] = None,
) -> list[dict[str, Any]]:
    """Institutions matching ``q``. Declares its own query parameters, so they
    still appear in the OpenAPI schema exactly as if the route named them."""
    return await search_institutions(session, q=q, limit=clamp_limit(limit))


InstitutionResultsDep = Annotated[list[dict[str, Any]], Depends(institution_results)]


async def lookup_page(
    session: SessionDep,
    catalogue: Annotated[str, Path(description="Which catalogue to list.")],
    q: Annotated[str | None, Query(description="Filter by display name."), StorableText] = None,
    limit: Annotated[int | None, Query(ge=1, le=LOOKUP_PAGE_SIZE)] = None,
    cursor: Annotated[str | None, Query(description="From a previous `next_cursor`.")] = None,
    common: Annotated[bool, Query(description="Only the common set — `languages` only.")] = False,
) -> tuple[list[dict[str, Any]], bool]:
    """One page of a lookup catalogue, and whether another follows."""
    if catalogue not in LOOKUPS:
        # 404 rather than 422: `/catalog/nonsense` is a URL that does not exist.
        raise NotFoundError(f"no catalogue named {catalogue!r}")
    return await list_lookup(
        session,
        catalogue,
        q=q,
        common=common,
        # A bigger default than the shared one: this serves select boxes, and
        # `countries` is 249 rows a client wants in a single call.
        limit=min(limit or LOOKUP_PAGE_SIZE, LOOKUP_PAGE_SIZE),
        cursor=decode_cursor(cursor),
    )


LookupPageDep = Annotated[tuple[list[dict[str, Any]], bool], Depends(lookup_page)]


async def target_education(user_id: TargetUserDep, session: SessionDep) -> list[dict[str, Any]]:
    return await list_education(session, user_id)


async def target_goal(user_id: TargetUserDep, session: SessionDep) -> dict[str, Any] | None:
    return await get_goal(session, user_id)


async def target_awards(user_id: TargetUserDep, session: SessionDep) -> list[dict[str, Any]]:
    return await list_awards(session, user_id)


async def target_mentor_profile(
    user_id: TargetUserDep, session: SessionDep
) -> dict[str, Any] | None:
    return await get_mentor_profile(session, user_id)


EducationDep = Annotated[list[dict[str, Any]], Depends(target_education)]
GoalDep = Annotated[dict[str, Any] | None, Depends(target_goal)]
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
        "goal": await get_goal(session, user_id),
        "awards": await list_awards(session, user_id),
        "mentor_profile": await get_mentor_profile(session, user_id),
    }


OwnAttributesDep = Annotated[dict[str, Any], Depends(own_attributes)]


# The write side, bound here for the same reason the reads are: `api/` may not
# import `infra/`. Each dependency declares its own request body, so the payload
# still appears in the OpenAPI schema exactly as if the route named it, and a
# route module never learns that a writer exists.
#
# **Each one commits.** A route that forgot would answer 201 and persist
# nothing. Putting the commit beside the write leaves no second place to forget
# it — and for education, the institution and the entry are both written before
# that commit, which is what makes them one transaction.


async def created_education(
    payload: EducationWrite, user_id: OwnerDep, session: SessionDep
) -> tuple[UUID, bool]:
    result = await create_education(session, user_id, payload.model_dump())
    await session.commit()
    return result


async def updated_education(
    entry_id: UUID, payload: EducationPatch, user_id: OwnerDep, session: SessionDep
) -> bool:
    # `exclude_unset` is what makes this a PATCH: a field the client did not
    # send is absent, not null. Without it every omitted field would be written
    # as its default and a one-field edit would blank the rest.
    changed = await update_education(
        session, user_id, entry_id, payload.model_dump(exclude_unset=True)
    )
    await session.commit()
    return changed


async def deleted_education(entry_id: UUID, user_id: OwnerDep, session: SessionDep) -> bool:
    removed = await delete_education(session, user_id, entry_id)
    await session.commit()
    return removed


async def upserted_goal(payload: GoalWrite, user_id: OwnerDep, session: SessionDep) -> UUID:
    """One goal per user, so this replaces rather than appends."""
    goal_id: UUID = await upsert_goal(session, user_id, payload.model_dump(exclude_unset=True))
    await session.commit()
    return goal_id


async def deleted_goal(user_id: OwnerDep, session: SessionDep) -> bool:
    removed = await delete_goal(session, user_id)
    await session.commit()
    return removed


async def created_award(payload: AwardWrite, user_id: OwnerDep, session: SessionDep) -> UUID:
    award_id = await create_award(session, user_id, payload.model_dump())
    await session.commit()
    return award_id


async def updated_award(
    award_id: UUID, payload: AwardPatch, user_id: OwnerDep, session: SessionDep
) -> bool:
    changed = await update_award(session, user_id, award_id, payload.model_dump(exclude_unset=True))
    await session.commit()
    return changed


async def deleted_award(award_id: UUID, user_id: OwnerDep, session: SessionDep) -> bool:
    removed = await delete_award(session, user_id, award_id)
    await session.commit()
    return removed


async def created_mentor_profile(
    payload: MentorProfileWrite, user_id: OwnerDep, session: SessionDep
) -> UUID:
    """A second application is a 409, not a second row.

    `uq_mentor_profiles_user_id` is what actually prevents the duplicate; this
    checks first so the caller gets a considered answer rather than a constraint
    violation surfacing as a 500.
    """
    existing = await session.execute(
        text("SELECT 1 FROM mentor_profiles WHERE user_id = :u AND deleted_at IS NULL"),
        {"u": user_id},
    )
    if existing.first() is not None:
        raise ConflictError("this user already has a mentor profile")

    profile_id = await create_mentor_profile(
        session, user_id, payload.model_dump(exclude_unset=True)
    )
    await session.commit()
    return profile_id


async def updated_mentor_profile(
    payload: MentorProfileWrite, user_id: OwnerDep, session: SessionDep
) -> bool:
    changed = await update_mentor_profile(session, user_id, payload.model_dump(exclude_unset=True))
    await session.commit()
    return changed


async def upserted_profile(
    payload: UserProfileWrite, user_id: OwnerDep, session: SessionDep
) -> None:
    await upsert_profile(session, user_id, payload.model_dump(exclude_unset=True))
    await session.commit()


CreatedEducationDep = Annotated[tuple[UUID, bool], Depends(created_education)]
UpdatedEducationDep = Annotated[bool, Depends(updated_education)]
DeletedEducationDep = Annotated[bool, Depends(deleted_education)]
UpsertedGoalDep = Annotated[UUID, Depends(upserted_goal)]
DeletedGoalDep = Annotated[bool, Depends(deleted_goal)]
CreatedAwardDep = Annotated[UUID, Depends(created_award)]
UpdatedAwardDep = Annotated[bool, Depends(updated_award)]
DeletedAwardDep = Annotated[bool, Depends(deleted_award)]
CreatedMentorProfileDep = Annotated[UUID, Depends(created_mentor_profile)]
UpdatedMentorProfileDep = Annotated[bool, Depends(updated_mentor_profile)]
UpsertedProfileDep = Annotated[None, Depends(upserted_profile)]


# --------------------------------------------------------------------------
# The admin surface
#
# **The control here is caller privilege, not row scoping** — which inverts
# every other guard in this module. Elsewhere the danger is one user reaching
# another's rows; here it is a caller with no grant reaching an action that
# changes somebody else's record. So the failure mode a test must chase is the
# opposite one, and there are four cases per endpoint rather than two.
#
# **A caller without the grant gets 404, not 403.** Same reasoning as everywhere
# else: 403 confirms the endpoint exists and that somebody may use it, which is
# exactly what an unprivileged caller should not learn.
# --------------------------------------------------------------------------


def require_admin(*roles: AdminRole) -> Callable[[dict[str, Any]], uuid.UUID]:
    """A dependency admitting only a caller holding one of ``roles``.

    A factory rather than one `AdminDep`, because `AdminRole` distinguishes what
    a grant is *for*: `mentor_approval` exists to approve mentors and says
    nothing about curating the catalogue. Treating every grant as equivalent
    would make the enum decorative — and this schema has removed a decorative
    column before.

    `super_admin` is admitted everywhere without being listed at each call site;
    spelling it out on every route is the kind of repetition that eventually
    disagrees with itself.
    """
    permitted = {AdminRole.SUPER_ADMIN, *roles}

    def dependency(user: CurrentUserDep) -> uuid.UUID:
        held = {str(role) for role in user["admin_roles"]}
        if not held & {str(role) for role in permitted}:
            raise NotFoundError("no such endpoint")
        # The acting admin's id: `approved_by` and `granted_by` want to know who
        # did it, and taking it from the token is the only answer a caller
        # cannot supply.
        return uuid.UUID(str(user["id"]))

    return dependency


#: Curating the catalogue — institutions. `limited_access` may look, not act.
CatalogueAdminDep = Annotated[uuid.UUID, Depends(require_admin())]

#: Approving mentors, which is what the `mentor_approval` grant is named for.
MentorAdminDep = Annotated[uuid.UUID, Depends(require_admin(AdminRole.MENTOR_APPROVAL))]

#: Reading either queue. Every live grant may look.
QueueViewerDep = Annotated[
    uuid.UUID,
    Depends(require_admin(AdminRole.MENTOR_APPROVAL, AdminRole.LIMITED_ACCESS)),
]


async def pending_institution_rows(
    _: CatalogueAdminDep,
    session: SessionDep,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
) -> list[dict[str, Any]]:
    return await pending_institutions(session, limit=min(limit or 50, 200))


async def approved_institution(
    institution_id: UUID, _: CatalogueAdminDep, session: SessionDep
) -> bool:
    changed = await approve_institution(session, institution_id)
    await session.commit()
    return changed


async def merged_institution(
    institution_id: UUID, payload: MergeRequest, _: CatalogueAdminDep, session: SessionDep
) -> int:
    """The repoint and the retirement commit together, or neither does."""
    moved = await merge_institution(
        session, losing_id=institution_id, winning_id=payload.winning_id
    )
    await session.commit()
    return moved


async def pending_mentor_rows(
    _: QueueViewerDep, session: SessionDep, limit: Annotated[int | None, Query(ge=1, le=200)] = None
) -> list[dict[str, Any]]:
    return await pending_mentors(session, limit=min(limit or 50, 200))


async def decided_mentor(
    user_id: UUID,
    payload: DeclineRequest,
    admin_id: MentorAdminDep,
    session: SessionDep,
    approve: Annotated[bool, Query(description="True to approve, false to decline.")] = True,
) -> bool:
    changed = await decide(
        session, user_id=user_id, admin_id=admin_id, approved=approve, reason=payload.reason
    )
    await session.commit()
    return changed


async def listed_mentor(
    user_id: UUID,
    payload: DeclineRequest,
    admin_id: MentorAdminDep,
    session: SessionDep,
    listed: Annotated[bool, Query(description="True to list, false to unlist.")] = True,
) -> bool:
    """An admin moving a mentor's listing without touching their approval."""
    changed = await set_listing(
        session, user_id=user_id, admin_id=admin_id, listed=listed, reason=payload.reason
    )
    await session.commit()
    return changed


async def mentor_history(
    user_id: UUID,
    _: QueueViewerDep,
    session: SessionDep,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
) -> list[dict[str, Any]]:
    return await history(session, user_id, limit=min(limit or 50, 200))


async def paused_self(user_id: OwnerDep, session: SessionDep) -> bool:
    paused = await pause(session, user_id=user_id)
    await session.commit()
    return paused


async def resumed_self(user_id: OwnerDep, session: SessionDep) -> bool:
    """Refused unless this mentor was the one who paused themselves.

    Checked here rather than in the route because the answer needs the database:
    it is the newest unlisting's reason, and an admin's unlisting must not be
    undoable by the person it concerns.
    """
    if not await may_self_resume(session, user_id):
        return False
    resumed = await resume(session, user_id=user_id)
    await session.commit()
    return resumed


PendingInstitutionsDep = Annotated[list[dict[str, Any]], Depends(pending_institution_rows)]
ApprovedInstitutionDep = Annotated[bool, Depends(approved_institution)]
MergedInstitutionDep = Annotated[int, Depends(merged_institution)]
PendingMentorsDep = Annotated[list[dict[str, Any]], Depends(pending_mentor_rows)]
DecidedMentorDep = Annotated[bool, Depends(decided_mentor)]
ListedMentorDep = Annotated[bool, Depends(listed_mentor)]
MentorHistoryDep = Annotated[list[dict[str, Any]], Depends(mentor_history)]
PausedSelfDep = Annotated[bool, Depends(paused_self)]
ResumedSelfDep = Annotated[bool, Depends(resumed_self)]


async def replaced_languages(
    payload: UserLanguagesWrite, user_id: OwnerDep, session: SessionDep
) -> None:
    await replace_languages(session, user_id, [entry.model_dump() for entry in payload.languages])
    await session.commit()


ReplacedLanguagesDep = Annotated[None, Depends(replaced_languages)]


@lru_cache(maxsize=1)
def get_storage() -> SupabaseStorage:
    """One storage client per process, like the engine and the verifier.

    A client per request would open a connection pool per request — the same
    reason `get_session_factory` is cached.
    """
    settings = get_settings()
    if settings.supabase_url is None or settings.supabase_service_role_key is None:
        raise ConfigurationError("Supabase storage is not configured")
    return SupabaseStorage(
        base_url=str(settings.supabase_url).rstrip("/"),
        service_role_key=settings.supabase_service_role_key.get_secret_value(),
        bucket=settings.supabase_storage_bucket,
        client=httpx.Client(timeout=UPLOAD_TIMEOUT),
    )


async def _store_image(
    kind: AssetKind, upload: UploadFile, user_id: UUID, session: SessionDep, request: Request
) -> str:
    """Validate, re-encode, store, point the profile at it, drop the old one.

    **The body was already read before this ran** — FastAPI parses the multipart
    form to resolve `UploadFile`, spooling it to disk first. So the limit that
    saves the transfer lives in `api/limits.py`, and the one here is the limit on
    the *image*: read one byte past the cap and refuse if it arrives, which needs
    no trust in a header.
    """
    payload = await upload.read(MAX_UPLOAD_BYTES + 1)
    if len(payload) > MAX_UPLOAD_BYTES:
        raise ValidationError("that file is larger than 5 MB")

    # **Both of these block, so both go to a worker thread.** Decoding and
    # resizing is CPU-bound and the storage client is synchronous `httpx` —
    # awaiting either inline stalls *every other request* on this worker for the
    # duration of a 5 MB round trip. This is what FastAPI does with a `def`
    # endpoint; the storage client stays synchronous because the asset migration
    # script uses the same class outside any event loop.
    image = await run_in_threadpool(process, payload, kind)
    path = object_path(user_id, kind, image.payload, image.content_type)

    storage: SupabaseStorage = getattr(request.app.state, "storage", None) or get_storage()
    url = await run_in_threadpool(storage.upload, path, image.payload, image.content_type)

    previous = await replace_url(session, user_id, kind, url)
    await session.commit()

    # **After the commit, and never fatal.** The profile already points at the
    # new object, so a failure here leaves an orphan rather than a broken
    # profile — and an upload that already succeeded must not report failure
    # because a cleanup did not.
    if previous and previous != url:
        old_path = storage.path_of(previous)
        if old_path is not None:
            with suppress(StorageError):
                await run_in_threadpool(storage.delete, old_path)

    return url


async def uploaded_avatar(
    request: Request,
    user_id: OwnerDep,
    session: SessionDep,
    file: Annotated[UploadFile, File(description="JPEG, PNG or WebP, up to 5 MB.")],
) -> str:
    return await _store_image(AssetKind.AVATAR, file, user_id, session, request)


async def uploaded_banner(
    request: Request,
    user_id: OwnerDep,
    session: SessionDep,
    file: Annotated[UploadFile, File(description="JPEG, PNG or WebP, up to 5 MB.")],
) -> str:
    return await _store_image(AssetKind.BANNER, file, user_id, session, request)


UploadedAvatarDep = Annotated[str, Depends(uploaded_avatar)]
UploadedBannerDep = Annotated[str, Depends(uploaded_banner)]


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------
#
# Reads take `TargetUserDep` and writes take `OwnerDep`, which is the one clause
# that separates "an admin reviewing a mentor's schedule" from "an admin
# silently editing it". The pair differ only by their name at the call site, so
# a reader comparing the two routes sees it without opening this module.
#
# **These stay owner-and-admin, and are no longer waiting for anything.** They
# were narrowed pending D20's middle clause — render if the viewer has a session
# with this mentor — which needed a `sessions` table to scope against. That table
# arrived, and settled decision #94 then **dropped the clause**: a mentee with a
# session sees *that session*, which carries the mentor's name since #93, so
# nothing breaks when a mentor pauses. What a stranger may see about a mentor is
# `GET /mentors/{handle}`; these routes answer a different question — what has
# this mentor *declared* — and that was only ever theirs and an admin's.


async def target_availability_rules(
    user_id: TargetUserDep, session: SessionDep
) -> list[dict[str, Any]]:
    return await list_rules(session, user_id)


async def target_availability_exceptions(
    user_id: TargetUserDep, session: SessionDep
) -> list[dict[str, Any]]:
    return await list_exceptions(session, user_id)


async def created_availability_rule(
    payload: AvailabilityRuleWrite, user_id: OwnerDep, session: SessionDep
) -> UUID:
    rule_id = await create_rule(session, user_id, payload.model_dump())
    await session.commit()
    return rule_id


async def updated_availability_rule(
    rule_id: UUID, payload: AvailabilityRulePatch, user_id: OwnerDep, session: SessionDep
) -> bool:
    changed = await update_rule(session, user_id, rule_id, payload.model_dump(exclude_unset=True))
    await session.commit()
    return changed


async def deleted_availability_rule(rule_id: UUID, user_id: OwnerDep, session: SessionDep) -> bool:
    removed = await delete_rule(session, user_id, rule_id)
    await session.commit()
    return removed


async def created_availability_exception(
    payload: AvailabilityExceptionWrite, user_id: OwnerDep, session: SessionDep
) -> UUID:
    exception_id = await create_exception(session, user_id, payload.model_dump())
    await session.commit()
    return exception_id


async def deleted_availability_exception(
    exception_id: UUID, user_id: OwnerDep, session: SessionDep
) -> bool:
    removed = await delete_exception(session, user_id, exception_id)
    await session.commit()
    return removed


AvailabilityRulesDep = Annotated[list[dict[str, Any]], Depends(target_availability_rules)]
AvailabilityExceptionsDep = Annotated[list[dict[str, Any]], Depends(target_availability_exceptions)]
CreatedAvailabilityRuleDep = Annotated[UUID, Depends(created_availability_rule)]
UpdatedAvailabilityRuleDep = Annotated[bool, Depends(updated_availability_rule)]
DeletedAvailabilityRuleDep = Annotated[bool, Depends(deleted_availability_rule)]
CreatedAvailabilityExceptionDep = Annotated[UUID, Depends(created_availability_exception)]
DeletedAvailabilityExceptionDep = Annotated[bool, Depends(deleted_availability_exception)]


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------
#
# **Two different scopes, and the difference is the URL.**
#
# The list is addressed by user — `/users/{id}/sessions` — so it takes
# `TargetUserDep`, and an admin reviewing somebody's sessions is the same
# implementation as that person reading their own.
#
# The single session and its events are addressed by session id, with no user in
# the path. There is no target user to resolve, so the scope is the **caller**:
# the query asks for a session this caller is a party to, and a session they are
# not party to is indistinguishable from one that does not exist. An admin is
# **not** admitted here — `/sessions/{id}` carries no user whose records an admin
# could be said to be reviewing, and widening it later is additive where
# narrowing would be breaking.


async def target_sessions(
    user_id: TargetUserDep,
    session: SessionDep,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int | None, Query(ge=1, le=MAX_PAGE_SIZE)] = None,
) -> tuple[list[dict[str, Any]], bool]:
    """One page of the sessions a user is a party to."""
    return await list_sessions(
        session, user_id, limit=clamp_limit(limit), cursor=decode_cursor(cursor)
    )


async def viewer_session(
    session_id: UUID, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    """One session, or a 404 that does not say which kind of 404 it is."""
    row = await get_session_row(session, session_id, user["id"])
    if row is None:
        raise NotFoundError("no such session")
    return row


async def viewer_session_events(
    session_id: UUID, user: CurrentUserDep, session: SessionDep
) -> list[dict[str, Any]]:
    """One session's history, scoped through the session itself.

    ``None`` from the store means the session is not the caller's, which is a
    404. An **empty list** means it is theirs and has no history — a different
    claim, and one that must not be used to answer the first case.
    """
    rows = await list_session_events(session, session_id, user["id"])
    if rows is None:
        raise NotFoundError("no such session")
    return rows


# --------------------------------------------------------------------------
# Bookable slots
# --------------------------------------------------------------------------
#
# **The one dependency in this module with no viewer.** Every other read here
# resolves a caller and scopes to them; this one is public, and what stands in
# place of a viewer is the mentor's own state — approved *and* listed, checked
# inside the query. The absence of `CurrentUserDep` below is the whole
# authorization decision, so it is stated rather than left to be noticed.


async def mentor_slots(
    user_id: UUID,
    session: SessionDep,
    session_type_id: Annotated[UUID, Query(description="Which offering to price the slots for.")],
    start: Annotated[
        dt.date | None,
        Query(description="First day, in the mentor's timezone. Defaults to their today."),
    ] = None,
    end: Annotated[
        dt.date | None,
        Query(
            description=(
                "Day after the last, exclusive. Defaults to "
                f"{DEFAULT_PROJECTION_DAYS} days after `start`."
            )
        ),
    ] = None,
) -> list[UtcInterval]:
    """Slots someone could book, or a 404 that says nothing about why.

    **`now` is read here and passed down**, rather than inside the store. The
    notice window makes this answer depend on the clock, and a function reading
    its own clock cannot be tested against a DST boundary without moving the
    machine's timezone.

    **The dates are not defaulted or validated here**, though this is the edge
    and that is where validation usually belongs. An omitted `start` means the
    mentor's today, which needs the mentor's timezone — so the default is only
    knowable after the query that finds them, and a range's legality depends on
    the default. Splitting the two would put half a rule in each layer.

    `session_type_id` stays **required**. A slot's length and notice window come
    from the offering, so "when is this mentor free" has no answer without one.
    Falling back to "their only offering" would break every caller that omitted
    it on the day a mentor adds a second — someone else's edit breaking an
    integration that did not change.
    """
    slots = await list_slots(
        session,
        user_id,
        session_type_id,
        start=start,
        end=end,
        now=dt.datetime.now(dt.UTC),
    )
    if slots is None:
        raise NotFoundError("no such bookable session type")
    return slots


# --------------------------------------------------------------------------
# The public mentor profile
# --------------------------------------------------------------------------


async def public_mentor(handle: str, session: SessionDep) -> dict[str, Any]:
    """One mentor, as a stranger sees them, or a 404 that says nothing about why.

    **Composed rather than re-queried.** The session types come from
    `list_session_types()` — the same function serving `/session-types` — so the
    inlined list and the standalone endpoint cannot drift. It re-checks the
    mentor's visibility, which is a second cheap statement rather than a
    "skip the check" variant, because a guard with a bypass parameter is a guard
    with a bypass.

    `handle` is an id or a slug and the store resolves either. It is not a `UUID`
    in the signature for that reason: FastAPI would refuse a slug at the door with
    a 422, which would tell a caller that the id they guessed was well-formed.
    """
    row = await get_public_mentor(session, handle)
    if row is None:
        raise NotFoundError("no such mentor")

    user_id = row["user_id"]
    # Six statements for one profile, and that is a decision rather than an
    # accident. Each list is a different table with a different scope, so they
    # cannot be one join without a fan-out to unpick in Python; and a profile is
    # a single-resource read a client makes once per page, not per row of a list.
    # The alternative — six round trips from the browser — is worse for the same
    # work. `mentor_page` is where a count like this would be a defect.
    return {
        "row": row,
        "offerings": (await offerings_for(session, [user_id])).get(user_id, []),
        "session_types": await list_session_types(session, user_id) or [],
        "education": await list_education(session, user_id),
        "scholarships": await list_awards(session, user_id),
        "languages": await list_languages(session, user_id),
        "stats": await mentor_stats(session, user_id),
    }


async def mentor_page(
    session: SessionDep,
    q: Annotated[
        str | None,
        Query(description="Search mentors by name, school, programme or country."),
        StorableText,
    ] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int | None, Query(ge=1, le=MAX_PAGE_SIZE)] = None,
) -> tuple[list[dict[str, Any]], bool, str | None]:
    """One page of bookable mentors, browsing or searching.

    **The mode decides how the token is read**, which is why decoding happens
    here rather than in the store: a browse cursor and a search cursor are both
    opaque base64 and are not interchangeable, so each decoder refuses the
    other's tag and a mixed request is a 422 rather than a confidently wrong
    page.

    The third element of the return is the mode, so the route knows which kind of
    token to mint without re-deriving it from `q` and risking the two disagreeing.
    """
    # Normalised **here and only here**. The store used to strip and test `q`
    # again, so a broken decision in this function was masked by the store's
    # copy quietly doing the right thing — one rule in two places, invisible
    # precisely because the two agreed. Now this decides and the store trusts.
    term = (q or "").strip() or None
    if term is not None:
        offset = decode_offset_cursor(cursor)
        rows, has_more = await search_mentors(
            session, limit=clamp_limit(limit), q=term, offset=offset
        )
        # `next_offset_cursor`, not `encode_offset_cursor`: past the depth cap
        # there is no next page, and minting one the decoder then refuses ends a
        # deep search on a 422 for a client that followed the envelope exactly.
        return rows, has_more, next_offset_cursor(offset + len(rows)) if has_more else None

    rows, has_more = await search_mentors(
        session, limit=clamp_limit(limit), after=decode_id_cursor(cursor)
    )
    return rows, has_more, encode_id_cursor(rows[-1]["cursor_id"]) if has_more and rows else None


MentorPageDep = Annotated[tuple[list[dict[str, Any]], bool, str | None], Depends(mentor_page)]

PublicMentorDep = Annotated[dict[str, Any], Depends(public_mentor)]

SlotsDep = Annotated[list[UtcInterval], Depends(mentor_slots)]


async def mentor_session_types(user_id: UUID, session: SessionDep) -> list[dict[str, Any]]:
    """What a mentor offers, or a 404 that does not say which kind of 404 it is.

    No `CurrentUserDep`, and that absence is the whole authorization decision —
    the mentor's own state stands in for a viewer, checked inside the query.

    `None` from the store means the mentor is not publicly visible. An **empty
    list** means they are, and are offering nothing bookable — a different claim,
    and one that must not be used to answer the first.
    """
    rows = await list_session_types(session, user_id)
    if rows is None:
        raise NotFoundError("no such mentor")
    return rows


SessionTypesDep = Annotated[list[dict[str, Any]], Depends(mentor_session_types)]


async def own_session_types(user: CurrentUserDep, session: SessionDep) -> list[dict[str, Any]]:
    """The caller's own offerings, including the ones they have switched off.

    **No authorization argument, and no `TargetUserDep`.** `CurrentUserDep` *is*
    the caller, so there is no target to resolve and nothing to admit an admin
    through — the same shape as `own_attributes` above. The scope is the caller's
    id spread into the store's `WHERE`, which is the only guard on this read.

    `user["id"]` rather than an attribute: `get_current_user` returns a plain
    `dict[str, Any]` built from a `text()` row, so nothing here is a typed model
    and a wrong key would be a `KeyError` at runtime rather than a validation
    error. The key is `SELECT`ed unconditionally by `CURRENT_USER`, so it is
    present whenever this runs — the same assumption every other dependency in
    this module already makes.
    """
    return await list_own_session_types(session, user["id"])


OwnSessionTypesDep = Annotated[list[dict[str, Any]], Depends(own_session_types)]


async def created_own_session_type(
    payload: MentorSessionTypeWrite, user: CurrentUserDep, session: SessionDep
) -> UUID:
    """The offering and its booking config, in one transaction.

    **One commit, after both inserts.** `/slots` and both read paths inner-join
    `session_type_booking_configs`, so an offering without one is invisible
    everywhere and unbookable, and nothing writes a config on its own — a commit
    between the two statements would make that state reachable and permanent.

    `CurrentUserDep` rather than `OwnerDep`: there is no `{user_id}` in the path
    to resolve, so the caller *is* the scope, matching `own_session_types` above.
    """
    session_type_id = await create_session_type(session, user["id"], payload.model_dump())
    if session_type_id is None:
        raise NotFoundError("this user has no mentor profile")
    await session.commit()
    return session_type_id


async def updated_own_session_type(
    session_type_id: UUID,
    payload: MentorSessionTypePatch,
    user: CurrentUserDep,
    session: SessionDep,
) -> bool:
    """`exclude_unset` is what makes this a PATCH: a field the client did not send
    is absent, not null. Without it every omitted field is written as its default
    and a one-field edit blanks the rest."""
    changed = await update_session_type(
        session, user["id"], session_type_id, payload.model_dump(exclude_unset=True)
    )
    await session.commit()
    return changed


CreatedOwnSessionTypeDep = Annotated[UUID, Depends(created_own_session_type)]
UpdatedOwnSessionTypeDep = Annotated[bool, Depends(updated_own_session_type)]

SessionsPageDep = Annotated[tuple[list[dict[str, Any]], bool], Depends(target_sessions)]
SessionDetailDep = Annotated[dict[str, Any], Depends(viewer_session)]
SessionEventsDep = Annotated[list[dict[str, Any]], Depends(viewer_session_events)]
