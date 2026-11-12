"""Settings, the database session, the caller, and the shared request plumbing."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from types import SimpleNamespace
from typing import Annotated, Any
from uuid import UUID

import httpx
from fastapi import Depends, Header, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.core.config import MIN_BOOKING_WINDOW_DAYS, Settings, get_settings
from app.core.errors import (
    AccountExistsError,
    AuthenticationError,
    ConfigurationError,
    ConflictError,
    NotFoundError,
    PreviewEndedError,
    PreviewNotFoundError,
    ReadOnlyViewAsError,
    ValidationError,
)
from app.core.reporting import remember_member
from app.domain.admin_permissions import EVERY_GRANT, PREVIEW, holds_any
from app.domain.availability import BookingWindow, booking_window
from app.domain.credits import CreditLadder, credit_ladder
from app.domain.enums import AdminRole
from app.domain.idempotency import request_fingerprint
from app.domain.sessions import response_window_hours
from app.infra.auth.supabase import SupabaseTokenVerifier, TokenClaims, issuer_for
from app.infra.db import preview_store
from app.infra.db.analytics_scope import sendable
from app.infra.db.engine import create_database_engine, create_session_factory
from app.infra.db.first_sign_in import provision_first_sign_in, record_auth_method
from app.infra.db.idempotency import Held, Mismatched, Replayed, reserve
from app.infra.db.models.user import User
from app.infra.db.predicates import migrated
from app.infra.db.preview_store import Owner

# `get_session` is aliased: this module already has one, and it is the **database
# session** dependency at line 142. Two callables with that name in one file is a
# collision a reader resolves by scrolling, and the wrong one is a plausible
# mistake rather than an obvious error — `bubble_id` shadowed a local the same
# way in the M4 transform and raised `UnboundLocalError` far from the edit.
from app.infra.storage.supabase import SupabaseStorage, image_storage_for

logger = logging.getLogger(__name__)

#: How long a storage call may take before the request gives up.
#:
#: Longer than a database call and much shorter than a browser's patience: the
#: object is at most 5 MB and Supabase is a network hop, so a call still running
#: after this is a call that has failed and not yet said so.
UPLOAD_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

#: `Cache-Control` for a response only its caller may hold: their own figures,
#: a private file. One value, so tightening it reaches every such route.
PRIVATE_NO_STORE = "private, no-store"

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
        issuer=issuer_for(settings.supabase_url),
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
    async with _session_factory(request)() as session:
        yield session


def _session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """This app's session factory: `app.state`'s when it carries one."""
    factory: async_sessionmaker[AsyncSession] = (
        getattr(request.app.state, "session_factory", None) or get_session_factory()
    )
    return factory


@asynccontextmanager
async def _own_transaction(request: Request) -> AsyncIterator[AsyncSession]:
    """A session apart from the request's, committed on leaving cleanly: for a
    record that must stand whatever the request does next, without committing
    anything the request itself has started."""
    async with _session_factory(request)() as session:
        yield session
        await session.commit()


SessionDep = Annotated[AsyncSession, Depends(get_session)]

#: Seconds the database has to answer `/ready` before the process is reported
#: not ready (decision 243).
READY_TIMEOUT = 2.0


async def database_ready(session: SessionDep) -> bool:
    """Whether the database answers one row within `READY_TIMEOUT`. Here, not
    in the route, because only the wiring layer may speak SQL."""
    try:
        await asyncio.wait_for(session.execute(text("SELECT 1")), timeout=READY_TIMEOUT)
    except Exception:
        # The cause is for the operator; the caller learns only that it failed.
        logger.warning("readiness check failed", extra={"check": "database"}, exc_info=True)
        return False
    return True


DatabaseReadyDep = Annotated[bool, Depends(database_ready)]


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
    # In a thread: a key-set refetch is a blocking HTTP call, and on the event
    # loop it would stall every request in the process while it waits.
    return await asyncio.to_thread(verifier.verify, credentials.credentials)


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
#
# **Keyed by exactly one of `:auth_id` (the token's subject) and `:user_id`**
# (a preview's user, decision 313), the other `NULL`: one column list for the
# caller and for the user a preview shows (`_user_row`).
#
# `oc` and `cc` are the caller's own origin and current countries, for `/me`
# only: primary-key joins off the caller's own profile row, so they add no
# query and widen no scope.
#
# `analytics_consent_*` is the caller's analytics answer (unique per user, so
# one indexed join): `/me` returns it (`analytics_consent_of`), and an error
# report follows it without querying a database that may be what failed,
# together with `analytics_sendable`, `analytics_scope.sendable` itself
# compiled onto `u` (decision 339).
# `is_migrated` is `predicates.migrated` compiled the same way (decision 340).
_SENDABLE, _MIGRATED = (
    predicate(aliased(User, name="u")).compile(
        dialect=postgresql.dialect(),  # type: ignore[no-untyped-call]
        compile_kwargs={"literal_binds": True},
    )
    for predicate in (sendable, migrated)
)
# What keeps the S608/B608 suppression below true: the predicates bind nothing.
if _SENDABLE.params or _MIGRATED.params:
    raise RuntimeError("the CURRENT_USER predicates must compile without bound values")
# S608: the only interpolations are `_SENDABLE` and `_MIGRATED`, our own
# predicates compiled at import with no bound values; nothing from a request
# reaches this text.
CURRENT_USER = text(f"""
    SELECT u.id, u.email, u.first_name, u.last_name, u.slug, u.primary_role,
           u.timezone, u.email_verified_at, u.created_at, u.auth_method,
           ({_MIGRATED}) AS is_migrated,
           p.about_me, p.about_me_drafted_at, p.gender, p.avatar_url, p.banner_url,
           p.avatar_focus_x, p.avatar_focus_y,
           p.social_linkedin,
           p.cover_color, p.cover_art,
           oc.code AS origin_country_code, oc.display_name AS origin_country_name,
           cc.code AS current_country_code, cc.display_name AS current_country_name,
           (p.user_id IS NOT NULL) AS has_profile,
           COALESCE(a.roles, ARRAY[]::text[]) AS admin_roles,
           (a.roles IS NOT NULL) AS is_admin,
           ac.granted AS analytics_consent_granted,
           ac.version AS analytics_consent_version,
           ac.updated_at AS analytics_consent_updated_at,
           ({_SENDABLE}) AS analytics_sendable
    FROM users u
    LEFT JOIN user_profiles p ON p.user_id = u.id
    LEFT JOIN analytics_consents ac ON ac.user_id = u.id
    LEFT JOIN countries oc ON oc.id = p.origin_country_id
    LEFT JOIN countries cc ON cc.id = p.current_country_id
    LEFT JOIN LATERAL (
        SELECT array_agg(g.admin_role::text) AS roles
        FROM admin_users g
        WHERE g.user_id = u.id AND g.revoked_at IS NULL
    ) a ON TRUE
    WHERE (u.auth_id = :auth_id OR u.id = :user_id) AND u.deleted_at IS NULL
""")  # noqa: S608  # nosec B608


def analytics_consent_of(user: dict[str, Any]) -> Any | None:
    """The caller's analytics answer (`granted`, `version`, `updated_at`) from
    their `CURRENT_USER` row, or ``None`` when they never gave one: what `/me`
    returns, read with the caller rather than by a second query."""
    if user["analytics_consent_version"] is None:
        return None
    return SimpleNamespace(
        granted=user["analytics_consent_granted"],
        version=user["analytics_consent_version"],
        updated_at=user["analytics_consent_updated_at"],
    )


async def _user_row(
    session: AsyncSession, *, auth_id: UUID | None = None, user_id: UUID | None = None
) -> dict[str, Any] | None:
    """`CURRENT_USER` by exactly one of a token's subject or our own user id."""
    result = await session.execute(
        CURRENT_USER,
        {"auth_id": auth_id, "user_id": user_id},
    )
    row = result.mappings().first()
    return None if row is None else dict(row)


async def _signed_in_user(claims: TokenClaims, session: AsyncSession) -> dict[str, Any]:
    """The token's own user, created on a first sign-in (decision 178)."""
    row = await _user_row(session, auth_id=claims.subject)
    if row is None and claims.email:
        await provision_first_sign_in(
            session, auth_id=claims.subject, email=claims.email, auth_method=claims.auth_method
        )
        row = await _user_row(session, auth_id=claims.subject)
    if row is None:
        raise NotFoundError("no account is linked to this identity")
    if row["auth_method"] is None and claims.auth_method is not None:
        # A migrated account's first sign-in here (PRD 30 A7): once, then never.
        await record_auth_method(session, row["id"], claims.auth_method)
        row["auth_method"] = claims.auth_method
    return row


# --------------------------------------------------------------------------
# Preview as user (decision 313)
#
# **The swap lives in the one step every authenticated route resolves**, so no
# router can forget it: with `X-EF-Preview` the caller is the previewed user,
# read only. What the preview records (its end, its activity, a refused write)
# is committed in a transaction of its own, so it stands, and the request's own
# work is never committed with it.
# --------------------------------------------------------------------------

#: The request header naming the caller's live preview.
PREVIEW_HEADER = "X-EF-Preview"

#: The methods a preview may use; every other one is refused.
READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

PreviewHeaderDep = Annotated[
    str | None,
    Header(
        alias=PREVIEW_HEADER,
        description=(
            "An admin's live preview session id (decision 313), sent beside the "
            "admin's own bearer token: the request is answered as the previewed "
            "user, read only. `404 /problems/preview-not-found` when it names no "
            "preview of this admin, the admin lacks the preview permission, or "
            "previews are off, alike for each; `401` "
            "`/problems/preview-ended` once the preview has ended."
        ),
    ),
]


@dataclass(frozen=True, slots=True)
class LivePreview:
    """The live preview a request is served in, on `request.state.preview`."""

    id: UUID
    owner: Owner
    target_user_id: UUID
    #: When the current user's messages were revealed; ``None`` until then.
    revealed_at: datetime | None


def preview_switched_on(request: Request) -> bool:
    """Whether this app runs with previews on (`PREVIEW_AS_USER_ENABLED`)."""
    return _configured(request).preview_as_user_enabled


def in_preview(request: Request) -> bool:
    """**The one test for "this request is served in a preview"**, read by the
    write guard and by `refuse_in_preview`."""
    return getattr(request.state, "preview", None) is not None


async def _refuse(request: Request) -> None:
    """Log the refused request as `blocked_write` (its method and route
    template) in its own transaction, then answer `403 read-only-view-as`."""
    preview: LivePreview = request.state.preview
    async with _own_transaction(request) as own:
        row = await preview_store.load(own, preview.owner, preview.id)
        if row is not None and row.ended_at is None:
            await preview_store.log_blocked_write(
                own, row, request.method, request.scope["route"].path
            )
    raise ReadOnlyViewAsError("a preview is read only")


async def _previewed_user(
    request: Request,
    claims: TokenClaims,
    session: AsyncSession,
    admin: dict[str, Any],
    header: str,
) -> dict[str, Any]:
    """The user the admin's preview shows, or a refusal.

    `404 /problems/preview-not-found`, one answer for every case so none is
    told from another, when previews are off, the admin lacks the PREVIEW
    permission, or the header names no preview of theirs (a malformed id names
    none). A preview that has lapsed, or that this admin started in another
    sign-in, is ended and answered `401 preview-ended`;
    one that had already ended answers the same, with the reason it ended.
    """
    if not preview_switched_on(request) or not holds_any(admin["admin_roles"], PREVIEW):
        raise PreviewNotFoundError
    try:
        preview_id = UUID(header)
    except ValueError as exc:
        raise PreviewNotFoundError from exc
    async with _own_transaction(request) as own:
        row = await preview_store.for_header(own, admin["id"], preview_id, claims.session_id)
        if row is not None and row.ended_at is None:
            await preview_store.touch(own, Owner(admin["id"], row.auth_session_id), preview_id)
    if row is None:
        raise PreviewNotFoundError
    if row.ended_at is not None:
        raise PreviewEndedError(end_reason=str(row.end_reason), session_id=str(row.id))
    request.state.preview = LivePreview(
        id=preview_id,
        owner=Owner(admin["id"], row.auth_session_id),
        target_user_id=row.target_user_id,
        revealed_at=row.revealed_at,
    )
    if request.method not in READ_ONLY_METHODS and in_preview(request):
        await _refuse(request)
    target = await _user_row(session, user_id=row.target_user_id)
    if target is None:
        raise NotFoundError("no account is linked to this identity")
    # Previewable users hold no grant; one given since is not part of the view.
    return target | {"is_admin": False, "admin_roles": []}


async def _resolve(
    request: Request, claims: TokenClaims, session: AsyncSession, header: str | None
) -> dict[str, Any]:
    user = await _signed_in_user(claims, session)
    if header is None:
        remember_member(
            request.state,
            user["id"],
            granted=user["analytics_consent_granted"],
            sendable=user["analytics_sendable"],
        )
        return user
    # In a preview no id is kept, the previewed user's or the admin's: an
    # error there is the admin's view, not either member's (decision 339).
    return await _previewed_user(request, claims, session, user, header)


async def get_current_user(
    request: Request, claims: ClaimsDep, session: SessionDep, preview: PreviewHeaderDep = None
) -> dict[str, Any]:
    """Resolve a verified token to the user it belongs to, creating it on first sign-in.

    **A first sign-in creates the account** (settled decision #178), from the
    token's `sub` and `email` — never by linking to an existing account by
    email, which is refused as `AccountExistsError`. See `first_sign_in`.

    **A valid token that still has no live account is a 404, not a 401**: one
    with no email to build an account from, or whose account was deleted. The
    token is genuine; no live account is linked to it.

    **With `X-EF-Preview` the caller is the previewed user, read only**
    (decision 313, `_previewed_user`): any method but GET, HEAD and OPTIONS is
    refused here, before a route's own work runs.
    """
    return await _resolve(request, claims, session, preview)


CurrentUserDep = Annotated[dict[str, Any], Depends(get_current_user)]


async def refuse_in_preview(request: Request, user: CurrentUserDep) -> None:
    """Refuse this route in a preview whatever its method: for a read that
    shows what a preview must not, or hands out the means to write (a sealed
    consent link). The same `403` and `blocked_write` as any write."""
    del user  # resolved first: it is what puts a preview on the request
    if in_preview(request):
        await _refuse(request)


RefuseInPreviewDep = Annotated[None, Depends(refuse_in_preview)]


async def messages_hidden(request: Request, user: CurrentUserDep) -> bool:
    """**The one rule for "message contents are hidden from this request"**
    (decision 314): it is served in a preview whose user's messages the admin
    has not revealed. Every message read and the message file download read
    it. A switch clears a reveal, so a reveal holds for the user it was for."""
    del user  # resolved first: it is what puts a preview on the request
    return in_preview(request) and request.state.preview.revealed_at is None


MessagesHiddenDep = Annotated[bool, Depends(messages_hidden)]


async def optional_viewer(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    session: SessionDep,
    preview: PreviewHeaderDep = None,
) -> UUID | None:
    """Who is asking, on an endpoint that answers anyone — or ``None``.

    **Any problem with the token reads as anonymous, never as an error.** The
    endpoints using this answer anybody, so a token can only ever *add* the
    owner's view — it must not take the public one away. An expired token (a
    tab resumed after Supabase's hour, before the refresh lands), a genuine
    token with no live account, and one whose email belongs to another account
    all get exactly what a visitor with no token gets. A `401`, `404` or `409`
    here would break a page anyone may read, for a reason about the caller.

    **Except in a preview** (decision 313): with `X-EF-Preview` the viewer is
    the previewed user, resolved exactly as `get_current_user` resolves one,
    refusals included. The admin asked for one person's view, and an anonymous
    page in its place would show the wrong view without saying so; an ended
    preview reaches the client as `401 preview-ended` here as everywhere.

    Resolved through `_resolve`, so first sign-in, the account rules and the
    preview are the same code as everywhere else rather than a second copy.
    """
    if preview is None and (credentials is None or not credentials.credentials):
        return None
    try:
        claims = await get_claims(request, credentials)
        user = await _resolve(request, claims, session, preview)
    except AuthenticationError, NotFoundError, AccountExistsError:
        if preview is not None:
            raise
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
    disagrees with itself. The rule is `holds_any`, which the flags that tell a
    caller what they may do read too.
    """

    def dependency(user: CurrentUserDep) -> uuid.UUID:
        if not holds_any(user["admin_roles"], roles):
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
#: **Every grant (`EVERY_GRANT`), which is what "any platform admin" costs here**,
#: the one set the queue gate and the preview permissions read too.
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
CreditAdminDep = Annotated[uuid.UUID, Depends(require_admin(*EVERY_GRANT))]

#: Approving mentors, which is what the `mentor_approval` grant is named for.
MentorAdminDep = Annotated[uuid.UUID, Depends(require_admin(AdminRole.MENTOR_APPROVAL))]

#: Reading either queue. Every live grant may look.
QueueViewerDep = Annotated[uuid.UUID, Depends(require_admin(*EVERY_GRANT))]


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
    client = httpx.Client(timeout=UPLOAD_TIMEOUT)
    storage = image_storage_for(get_settings(), client)
    if storage is None:
        # Not cached when it raises, so an unclosed client would leak per request.
        client.close()
        raise ConfigurationError("Supabase storage is not configured")
    return storage


ENDPOINT_BOOKING = "POST /api/v1/sessions"

#: Moving a session books one too, so it has its own fingerprint (decision 300).
ENDPOINT_MOVE = "POST /api/v1/sessions/{session_id}/move"

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


def _response_hours(request: Request) -> int:
    """This app's response window in whole hours (decision 278), for the routes
    that publish it."""
    return response_window_hours(_configured(request))


ResponseWindowHoursDep = Annotated[int, Depends(_response_hours)]


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
