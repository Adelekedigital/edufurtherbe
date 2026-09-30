"""Settings, the database session, the caller, and the shared request plumbing."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Callable
from functools import lru_cache
from typing import Annotated, Any
from uuid import UUID

import httpx
from fastapi import Depends, Header, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import MIN_BOOKING_WINDOW_DAYS, Settings, get_settings
from app.core.errors import (
    AccountExistsError,
    AuthenticationError,
    ConfigurationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.domain.availability import BookingWindow, booking_window
from app.domain.credits import CreditLadder, credit_ladder
from app.domain.enums import AdminRole
from app.domain.idempotency import request_fingerprint
from app.infra.auth.supabase import SupabaseTokenVerifier, TokenClaims
from app.infra.db.engine import create_database_engine, create_session_factory
from app.infra.db.first_sign_in import provision_first_sign_in
from app.infra.db.idempotency import Held, Mismatched, Replayed, reserve

# `get_session` is aliased: this module already has one, and it is the **database
# session** dependency at line 142. Two callables with that name in one file is a
# collision a reader resolves by scrolling, and the wrong one is a plausible
# mistake rather than an obvious error — `bubble_id` shadowed a local the same
# way in the M4 transform and raised `UnboundLocalError` far from the edit.
from app.infra.storage.supabase import SupabaseStorage

logger = logging.getLogger(__name__)

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
           p.avatar_focus_x, p.avatar_focus_y,
           p.social_linkedin, p.social_twitter, p.social_youtube,
           p.cover_color, p.cover_art,
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
    """Resolve a verified token to the user it belongs to, creating it on first sign-in.

    **A first sign-in creates the account** (settled decision #178), from the
    token's `sub` and `email` — never by linking to an existing account by
    email, which is refused as `AccountExistsError`. See `first_sign_in`.

    **A valid token that still has no live account is a 404, not a 401**: one
    with no email to build an account from, or whose account was deleted. The
    token is genuine; no live account is linked to it.
    """
    result = await session.execute(CURRENT_USER, {"auth_id": claims.subject})
    row = result.mappings().first()
    if row is None and claims.email:
        await provision_first_sign_in(session, auth_id=claims.subject, email=claims.email)
        result = await session.execute(CURRENT_USER, {"auth_id": claims.subject})
        row = result.mappings().first()
    if row is None:
        raise NotFoundError("no account is linked to this identity")
    return dict(row)


CurrentUserDep = Annotated[dict[str, Any], Depends(get_current_user)]


async def optional_viewer(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    session: SessionDep,
) -> UUID | None:
    """Who is asking, on an endpoint that answers anyone — or ``None``.

    **Any problem with the token reads as anonymous, never as an error.** The
    endpoints using this answer anybody, so a token can only ever *add* the
    owner's view — it must not take the public one away. An expired token (a
    tab resumed after Supabase's hour, before the refresh lands), a genuine
    token with no live account, and one whose email belongs to another account
    all get exactly what a visitor with no token gets. A `401`, `404` or `409`
    here would break a page anyone may read, for a reason about the caller.

    Resolved through `get_current_user` itself, so first sign-in and the
    account rules are the same statement as everywhere else rather than a
    second copy.
    """
    if credentials is None or not credentials.credentials:
        return None
    try:
        claims = await get_claims(request, credentials)
        user = await get_current_user(claims, session)
    except AuthenticationError, NotFoundError, AccountExistsError:
        return None
    viewer: UUID = user["id"]
    return viewer


OptionalViewerDep = Annotated[UUID | None, Depends(optional_viewer)]


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

#: Putting credits into somebody's balance. **Every live grant may do it**, which
#: is a deliberate widening from the `super_admin` split #220 uses for moderation:
#: removing a review from a public profile is curation, where crediting somebody
#: is support work that whoever is on shift has to be able to do.
#:
#: **Every role listed, which is what "any platform admin" costs here.**
#: `require_admin()` with no arguments admits `super_admin` *only* — the empty
#: call reads like "any admin" and is the narrowest possible gate, which is how
#: the first version of this dependency shipped documented as one thing and
#: behaving as its opposite. The integration test for `limited_access` is what
#: caught it.
#:
#: **The asymmetry with review moderation is deliberate, and worth stating
#: because the two shipped a day apart.** Deciding a report is `super_admin`
#: only; crediting somebody is not. Three things separate them:
#:
#: *Reversibility.* Upholding a report sets `reviews.deleted_at` and takes
#: somebody's words off a public profile — the author is not told and cannot
#: undo it. A credit is a lot with an expiry, visible on the recipient's own
#: card, and every one is in `admin_credit_grants` with a name against it.
#:
#: *Who it lands on.* A moderation decision affects a third party — the mentor
#: being reviewed — who did not ask for it. A credit affects the person
#: receiving it, in their favour.
#:
#: *When it is needed.* Support work happens at whatever hour somebody is stuck;
#: a report can wait for the person whose judgement it is. Gating credits on one
#: role means a mentee wrongly charged waits for that person to wake up.
#:
#: Bounded rather than trusted: capped at the monthly grant per action,
#: soft-deleted recipients refused, and the whole history readable by every
#: admin — so a grant nobody can justify is one anybody can find.
CreditAdminDep = Annotated[
    uuid.UUID,
    Depends(require_admin(AdminRole.MENTOR_APPROVAL, AdminRole.LIMITED_ACCESS)),
]

#: Approving mentors, which is what the `mentor_approval` grant is named for.
MentorAdminDep = Annotated[uuid.UUID, Depends(require_admin(AdminRole.MENTOR_APPROVAL))]

#: Reading either queue. Every live grant may look.
QueueViewerDep = Annotated[
    uuid.UUID,
    Depends(require_admin(AdminRole.MENTOR_APPROVAL, AdminRole.LIMITED_ACCESS)),
]


#: The header every idempotent endpoint reads.
IdempotencyKeyHeader = Header(
    alias="Idempotency-Key",
    min_length=1,
    max_length=255,
    description="A value unique to this attempt. Retries must reuse it.",
)


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


ENDPOINT_BOOKING = "POST /api/v1/sessions"

#: The second endpoint with an idempotency key, and the second that moves
#: something like money. Kept beside the first so the two fingerprints are
#: obviously distinct — a shared endpoint string would let a booking key replay
#: a credit grant.
ENDPOINT_ADMIN_CREDITS = "POST /api/v1/admin/credits"

#: The two session-type creates (#196). Optional keys, unlike the two above:
#: nothing here is money, and requiring one would break every client already
#: creating offerings without it.
ENDPOINT_SESSION_TYPE = "POST /api/v1/me/session-types"
ENDPOINT_QUESTION = "POST /api/v1/me/session-types/{session_type_id}/questions"


async def claim_idempotency_key(
    session: AsyncSession, *, key: str, user_id: uuid.UUID, endpoint: str, body: Any
) -> Held | Replayed:
    """Claim `key` for this request, or return the answer it already has.

    **One place for the refusals every idempotent endpoint gives**: a key reused
    with a different body is a `422` naming the mistake, and a key whose first
    request is still running is a `409`. `body` is fingerprinted with the
    endpoint, so one key cannot replay one endpoint's answer on another.
    """
    reservation = await reserve(
        session,
        key=key,
        user_id=user_id,
        endpoint=endpoint,
        request_hash=request_fingerprint(endpoint, body),
    )
    if isinstance(reservation, Mismatched):
        raise ValidationError(
            "this Idempotency-Key was already used for a different request; "
            "use a new key, or resend the original body"
        )
    if isinstance(reservation, Held | Replayed):
        return reservation
    raise ConflictError("a request with this Idempotency-Key is still in flight")


#: **200, not 201, and that is a decision rather than an oversight.**
#:
#: `test_a_creating_route_sends_a_location` requires every `201` route to set
#: `Location`, and it is right to: *"a client that has to guess the URL of what
#: it just made is being told less than the response could tell it."*
#:
#: A bulk grant has no single URL to give. It creates one lot per recipient, in
#: as many places, and the body is a *report* — what landed, what did not — not a
#: representation of one created thing. Setting `Location: /api/v1/admin/credits`
#: would satisfy the letter of the rule by pointing at a collection that cannot
#: be read, which is worse than not pointing at all.
#:
#: So the status says what actually happened rather than the header lying about
#: it. The rule keeps its full force for routes that create one thing.
GRANTED = status.HTTP_200_OK

#: The one success code booking has. Stored on the key so a replay returns the
#: same status as the original, rather than a `200` this endpoint never issues.
CREATED = status.HTTP_201_CREATED


def _ladder(request: Request) -> CreditLadder:
    """This app's credit ladder, resolved through the settings seam.

    **One dependency rather than three call sites reaching for configuration.**
    `credit_ladder` takes a `Settings` precisely so the choice of *which*
    settings is made here — in the composition root — and `_configured` is the
    rule for that: an app built with explicit settings must not silently run on
    the process-wide environment cache.
    """
    return credit_ladder(_configured(request))


LadderDep = Annotated[CreditLadder, Depends(_ladder)]


def _booking_window(request: Request) -> BookingWindow:
    """This app's booking window (Round 5), through the same settings seam."""
    return booking_window(_configured(request))


BookingWindowDep = Annotated[BookingWindow, Depends(_booking_window)]


def window_out_of_range(days: int | None, window: BookingWindow) -> bool:
    """Whether a sent window is shorter than the minimum or longer than the maximum."""
    return days is not None and not MIN_BOOKING_WINDOW_DAYS <= days <= window.max_days


def refuse_window_out_of_range(
    days: int | None, window: BookingWindow, *, stored: int | None = None
) -> None:
    """A window outside `MIN_BOOKING_WINDOW_DAYS`..the configured maximum is a
    `422` at `/booking_window_days`.

    Here rather than in the schema, because a `Field(le=...)` is fixed at import
    and the maximum is configuration. The one check every write shares.

    **Resending the stored value is not a change**, so it passes: a form sends
    back every field it shows, and a mentor who set 40 while it was allowed must
    be able to edit something else after the maximum drops to 14. Reads clamp it
    meanwhile. A PATCH passes ``stored``; a create has no row, so it does not.
    """
    if window_out_of_range(days, window) and days != stored:
        message = f"booking_window_days must be {MIN_BOOKING_WINDOW_DAYS} to {window.max_days}"
        raise ValidationError(message, field_errors=(("/booking_window_days", message),))


def _configured(request: Request) -> Settings:
    """This app's settings, falling back to the process-wide cache.

    Every request-scoped dependency that reaches for configuration goes through
    here — including `_rooms` and `_calendar` above, which are defined earlier
    only because their section is. `get_settings()` is an `lru_cache` over the
    environment, so calling it
    directly means an app built with explicit settings — which is how every test
    builds one — runs on whatever the process happens to hold instead.
    """
    return getattr(request.app.state, "settings", None) or get_settings()
