"""FastAPI dependency wiring.

One of the two sanctioned composition points (the other is ``main.py``), and
exempt from the layer check for that reason — this is where concrete ``infra``
classes get bound to what the routes ask for.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import suppress
from functools import lru_cache
from typing import Annotated, Any
from uuid import UUID

import httpx
from fastapi import Depends, File, Header, Path, Query, Request, UploadFile, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AwareDatetime, BaseModel, ConfigDict
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from app.api.schemas.admin import DeclineRequest, MergeRequest
from app.api.schemas.admin_credits import AdminCreditGrantWrite
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
    encode_cursor,
    encode_id_cursor,
    next_offset_cursor,
)
from app.api.schemas.intake import QuestionPatch, QuestionWrite
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
from app.api.schemas.referrals import ReferralClaim, ReferralWrite
from app.api.schemas.review_reports import ReportDecisionWrite, ReviewReportWrite
from app.api.schemas.reviews import ReviewEdit, ReviewWrite
from app.api.schemas.session_types import MentorSessionTypePatch, MentorSessionTypeWrite
from app.api.schemas.sessions import (
    SessionBookingWrite,
    SessionCancellationWrite,
    SessionRead,
    SessionTransitionWrite,
)
from app.core.config import Settings, get_settings
from app.core.errors import (
    AuthenticationError,
    ConfigurationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.domain.assets import AssetKind, object_path
from app.domain.attendance import join_window
from app.domain.availability import DEFAULT_PROJECTION_DAYS, UtcInterval
from app.domain.credits import CreditLadder, credit_ladder
from app.domain.enums import AdminRole, MeetingProvider, MentorStatusType
from app.domain.idempotency import request_fingerprint
from app.domain.images import MAX_UPLOAD_BYTES, process
from app.domain.notifications import REMINDER_OFFSETS, SESSION_REMINDER_KINDS
from app.infra.auth.supabase import SupabaseTokenVerifier, TokenClaims
from app.infra.clients.meetings import (
    DailyRooms,
    GoogleCalendar,
    NullCalendar,
    NullRooms,
    VenueUnavailableError,
    consent_url,
    exchange_code,
)
from app.infra.clients.scheduler import (
    NullScheduler,
    QStashScheduler,
    UntrustedCallbackError,
    verify_callback,
)
from app.infra.clients.secrets import SealError, seal, sealed_value, unsealed_value
from app.infra.db.admin_credits import grant_credits, list_admin_grants
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
from app.infra.db.calendar_store import (
    MentorFreeBusy,
    NullFreeBusy,
    active_connection,
    connect,
    disconnect,
)
from app.infra.db.catalogue_store import LOOKUPS, list_lookup, search_institutions
from app.infra.db.credit_store import get_credit_summary
from app.infra.db.education_writer import create_education, delete_education, update_education
from app.infra.db.engine import create_database_engine, create_session_factory
from app.infra.db.idempotency import Held, Mismatched, Replayed, record_response, reserve
from app.infra.db.intake_store import (
    create_question,
    delete_question,
    list_questions,
    update_question,
)
from app.infra.db.mentor_public_store import get_public_mentor, get_public_mentor_id
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
from app.infra.db.onboarding_store import get_onboarding
from app.infra.db.onboarding_writer import OnboardingResult, complete_onboarding
from app.infra.db.own_review_reader import list_reviews_about
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
from app.infra.db.referral_store import list_referrals
from app.infra.db.referral_writer import claim_referral, create_referral
from app.infra.db.review_eligibility import reviewable_sessions
from app.infra.db.review_moderation import decide_report, list_reviews_for_moderation
from app.infra.db.review_reader import get_review_row, list_mentor_reviews
from app.infra.db.review_report_writer import report_review
from app.infra.db.review_stats import mentor_review_stats
from app.infra.db.review_writer import edit_review, write_review
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
    delete_session_type,
    list_own_session_types,
    list_session_types,
    update_session_type,
)
from app.infra.db.session_writer import (
    book_session,
    provision_meeting,
    record_arrival,
    release_meeting,
    remind_before_session,
    remind_if_still_waiting,
    schedule_session_reminders,
    transition,
)
from app.infra.db.slot_store import list_slots
from app.infra.jobs.manifest import RUNTIME_JOB_NAMES, schedule_id
from app.infra.jobs.runner import RuntimeJobs
from app.infra.storage.supabase import StorageError, SupabaseStorage

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


async def own_attributes(
    user: CurrentUserDep, session: SessionDep, ladder: LadderDep
) -> dict[str, Any]:
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
        # Fetched unconditionally and rendered conditionally. The predicate is
        # "has a mentee goal", which the `goal` fetch above already answers, so
        # branching here would mean ordering these two against each other for
        # one `SUM` against an indexed column.
        "credits": await get_credit_summary(session, user_id, ladder=ladder),
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


async def own_referrals(user: CurrentUserDep, session: SessionDep) -> Sequence[Any]:
    """The caller's own invites. No authorization argument — `CurrentUserDep`
    *is* the caller, so there is no target to check."""
    return await list_referrals(session, user["id"])


OwnReferralsDep = Annotated[Sequence[Any], Depends(own_referrals)]


async def created_referral(
    payload: ReferralWrite, user: CurrentUserDep, session: SessionDep
) -> Any:
    referral = await create_referral(session, user["id"], payload.invitee_email)
    await session.commit()
    return referral


CreatedReferralDep = Annotated[Any, Depends(created_referral)]


async def claimed_referral(
    payload: ReferralClaim, user: CurrentUserDep, session: SessionDep, ladder: LadderDep
) -> Any:
    """Attach the caller to an invite.

    **The claim does not qualify it.** Qualification happens when the invitee
    finishes onboarding, which is a different transaction and deliberately so:
    claiming is the invitee saying who invited them, and finishing is the work
    that earns the referrer anything.
    """
    referral = await claim_referral(session, user["id"], payload.code, ladder=ladder)
    await session.commit()
    return referral


ClaimedReferralDep = Annotated[Any, Depends(claimed_referral)]


async def own_onboarding(user: CurrentUserDep, session: SessionDep) -> Any:
    """The caller's onboarding record, or 404.

    No authorization argument: `CurrentUserDep` *is* the caller, so there is no
    target to check.
    """
    row = await get_onboarding(session, user["id"])
    if row is None:
        raise NotFoundError("onboarding has not been started")
    return row


OwnOnboardingDep = Annotated[Any, Depends(own_onboarding)]


async def completed_onboarding(
    user: CurrentUserDep, session: SessionDep, ladder: LadderDep
) -> OnboardingResult:
    """Mark the caller's onboarding finished and pay the starter credit.

    **One transaction, and the commit is here.** The completion and the grant
    are two facts that must not be separable: split across two transactions
    there is a state where somebody is marked complete and holds no credit, and
    nothing would revisit it — completion is recorded, so a retry is a no-op,
    and the credit is missing forever.

    No authorization argument: `CurrentUserDep` *is* the caller, so there is no
    target to check.
    """
    result = await complete_onboarding(session, user["id"], ladder=ladder)
    await session.commit()
    return result


CompletedOnboardingDep = Annotated[OnboardingResult, Depends(completed_onboarding)]


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


def _canonical_grant(payload: AdminCreditGrantWrite) -> dict[str, Any]:
    """The request as the endpoint actually treats it.

    Recipients sorted and deduplicated, because `grant_credits` does both and
    says so in the OpenAPI description. The fingerprint has to agree with the
    behaviour, or a legitimate retry looks like a new request.
    """
    body = payload.model_dump(mode="json")
    body["user_ids"] = sorted({str(user_id) for user_id in payload.user_ids})
    return body


async def granted_admin_credits(
    payload: AdminCreditGrantWrite,
    admin_id: CreditAdminDep,
    session: SessionDep,
    ladder: LadderDep,
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=255,
            description="A value unique to this grant attempt. Retries must reuse it.",
        ),
    ],
) -> tuple[dict[str, Any], int, bool]:
    """Reserve the key, write the lots, store the answer — one transaction.

    **Required rather than optional, for the reason booking gives**: this is
    money, and an optional header makes the guarantee opt-in for exactly the
    caller who most needs it. A double-submitted grant is not recoverable by
    the admin noticing — the credits are already spendable.

    **The key and the write commit together**, so a stored `201` for lots that
    were never inserted cannot replay ids of nothing.
    """
    reservation = await reserve(
        session,
        key=idempotency_key,
        user_id=admin_id,
        endpoint=ENDPOINT_ADMIN_CREDITS,
        # **Canonicalised, because the endpoint promises duplicates and order
        # do not matter.** `request_fingerprint` sorts object keys but leaves
        # list order alone, so a retry that deduped or reordered its
        # recipients — which the description tells clients is harmless —
        # hashes differently and is refused as a *different* request. The
        # admin then has no way to learn whether the first attempt landed,
        # which is the one question a retry is asking.
        request_hash=request_fingerprint(ENDPOINT_ADMIN_CREDITS, _canonical_grant(payload)),
    )
    if isinstance(reservation, Replayed):
        return reservation.body, reservation.status_code, True
    if isinstance(reservation, Mismatched):
        raise ValidationError(
            "this Idempotency-Key was already used for a different request; "
            "use a new key, or resend the original body"
        )
    if not isinstance(reservation, Held):
        raise ConflictError("a request with this Idempotency-Key is still in flight")

    result = await grant_credits(
        session,
        admin_id=admin_id,
        user_ids=tuple(payload.user_ids),
        quantity=payload.quantity,
        note=payload.note,
        ladder=ladder,
        now=dt.datetime.now(dt.UTC),
    )
    body = {
        "granted": [str(user_id) for user_id in result.granted],
        "unresolved": [str(user_id) for user_id in result.unresolved],
        "quantity": payload.quantity,
    }
    # The answer and the lots commit together, so a stored response cannot
    # survive a crash that lost the credits it describes.
    await record_response(session, reservation, status_code=GRANTED, body=body)
    await session.commit()
    return body, GRANTED, False


async def admin_grant_history(
    _: CreditAdminDep,
    session: SessionDep,
    # `le=MAX_PAGE_SIZE`, not the `le=200` the offset-paged admin queues use:
    # this one goes through `clamp_limit`, which caps at 50, so publishing 200
    # would advertise a bound the endpoint never satisfies.
    limit: Annotated[int | None, Query(ge=1, le=MAX_PAGE_SIZE)] = None,
    cursor: Annotated[str | None, Query(description="From a previous `next_cursor`.")] = None,
    granted_by: Annotated[
        uuid.UUID | None,
        Query(description="Narrow to one admin's grants."),
    ] = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """The history of admin credit grants, newest first.

    **Every admin sees every grant, including their own and each other's.** An
    audit only one person can read is not an audit — and the `granted_by` filter
    exists so somebody can narrow to their own without that being the default.

    Read by the same gate that writes: whoever may hand out credits may see what
    has been handed out, and splitting the two would let an admin create rows
    they cannot then review.
    """
    rows, has_more = await list_admin_grants(
        session,
        limit=clamp_limit(limit),
        after=decode_cursor(cursor),
        granted_by=granted_by,
    )
    # **Minted beside the decode**, the rule `mentor_page` states: issuing the
    # token where the sort key is known keeps the two halves of the codec from
    # drifting, which is the defect that once invalidated every cursor an
    # endpoint handed out.
    if not (has_more and rows):
        return rows, None
    last = rows[-1]
    return rows, encode_cursor(last["created_at"].isoformat(), last["id"])


AdminGrantHistoryDep = Annotated[
    tuple[list[dict[str, Any]], str | None], Depends(admin_grant_history)
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
    status: Annotated[
        list[MentorStatusType] | None,
        Query(description="Repeat to widen: `?status=approved&status=declined`."),
    ] = None,
    since: Annotated[
        AwareDatetime | None,
        Query(description="Only events at or after this instant. Inclusive."),
    ] = None,
    until: Annotated[
        AwareDatetime | None,
        Query(description="Only events strictly before this instant. Exclusive."),
    ] = None,
) -> list[dict[str, Any]]:
    """One mentor's transitions, narrowed.

    **The range is validated here rather than in the store**, unlike `/slots`.
    There, an omitted `start` means the mentor's today and the default is only
    knowable after the query that finds them, so legality could not be decided
    at the edge. Here both bounds are absolute instants a caller either sent or
    did not, and nothing downstream can change them — so the edge is where it
    belongs.
    """
    if since is not None and until is not None and until <= since:
        raise ValidationError("`until` must be after `since`")
    return await history(
        session,
        user_id,
        limit=min(limit or 50, 200),
        kinds=status or (),
        since=since,
        until=until,
    )


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
# Booking
# --------------------------------------------------------------------------


#: What the fingerprint and the stored row call this endpoint. One string, so a
#: replay can never be served across endpoints because two literals drifted.
def _rooms(request: Request) -> Any:
    """The room provider, or the null one.

    **Read off `app.state` rather than constructed here**, following
    `app.state.storage`: the composition root wires a real adapter when one
    exists, and everything else keeps working against a default that creates
    nothing. `main.py` and this module are the sanctioned wiring points.
    """
    wired = getattr(request.app.state, "meeting_rooms", None)
    if wired is not None:
        return wired
    # **Built per request rather than at startup**, so a key added to the
    # environment takes effect on the next deploy without a wiring change, and
    # so a test that sets no key gets the null adapter without unsetting
    # anything. The client it constructs is cheap; Daily is called at most twice
    # per session.
    key = _configured(request).daily_api_key
    return DailyRooms(key.get_secret_value()) if key else NullRooms()


def _calendar(request: Request) -> Any:
    wired = getattr(request.app.state, "calendar", None)
    if wired is not None:
        return wired
    settings = _configured(request)
    # **All three or none.** A refresh token is useless without the client that
    # minted it, and two of three configured is the shape most likely to be a
    # half-finished setup — failing to `NullCalendar` there is quieter than
    # failing every booking with an OAuth error.
    if not (
        settings.google_oauth_client_id
        and settings.google_oauth_client_secret
        and settings.google_calendar_refresh_token
    ):
        return NullCalendar()
    return GoogleCalendar(
        client_id=settings.google_oauth_client_id,
        client_secret=settings.google_oauth_client_secret.get_secret_value(),
        refresh_token=settings.google_calendar_refresh_token.get_secret_value(),
        calendar_id=settings.google_calendar_id,
    )


#: Where QStash is told to call back. One constant, because the value is
#: signed into the token QStash mints — so the path the scheduler publishes
#: and the path the verifier expects must be the same string, not two that
#: happen to agree.
REMINDER_CALLBACK_PATH = "/api/v1/callbacks/reminders"

ENDPOINT_BOOKING = "POST /api/v1/sessions"

#: The second endpoint with an idempotency key, and the second that moves
#: something like money. Kept beside the first so the two fingerprints are
#: obviously distinct — a shared endpoint string would let a booking key replay
#: a credit grant.
ENDPOINT_ADMIN_CREDITS = "POST /api/v1/admin/credits"

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


async def booked_session(
    payload: SessionBookingWrite,
    user: CurrentUserDep,
    session: SessionDep,
    request: Request,
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=255,
            description="A value unique to this booking attempt. Retries must reuse it.",
        ),
    ],
) -> tuple[dict[str, Any], int, bool]:
    """Reserve the key, book the hour, store the answer — one transaction.

    **Required rather than optional, which is the one deviation from Stripe.**
    Stripe treats the header as recommended, and it can: its clients are servers
    written once. The retry here is a phone on a bad connection, and an optional
    header makes the guarantee opt-in for exactly the caller who most needs it.
    Requiring it now is also the safe direction to be wrong in — relaxing a
    required header later breaks nobody, and requiring an optional one breaks
    every client.

    **The key and the session commit together**, because a stored `201` for a
    session that was never written would replay the id of nothing. That also
    makes a refusal clean: `book_session` rolls back on a conflict, so the
    reservation goes with it and the client's next attempt starts fresh instead
    of being told forever that a request is in flight.

    **Read back through `get_session`, scoped to the caller.** A second query,
    deliberately: the response is the same shape `GET /sessions/{id}` returns,
    assembled by the same code, so the two cannot drift — and building it from
    the insert's own values would mean composing the party join by hand at the
    one moment there is no row to read it from.
    """
    reservation = await reserve(
        session,
        key=idempotency_key,
        user_id=user["id"],
        endpoint=ENDPOINT_BOOKING,
        request_hash=request_fingerprint(ENDPOINT_BOOKING, payload.model_dump(mode="json")),
    )
    if isinstance(reservation, Replayed):
        return reservation.body, reservation.status_code, True
    if isinstance(reservation, Mismatched):
        raise ValidationError(
            "this Idempotency-Key was already used for a different request; "
            "use a new key, or resend the original body"
        )
    if not isinstance(reservation, Held):
        raise ConflictError("a request with this Idempotency-Key is still in flight")

    session_id = await book_session(
        session,
        user["id"],
        payload.model_dump(),
        now=dt.datetime.now(dt.UTC),
        scheduler=_scheduler(request),
        callback_url=_reminder_callback_url(request),
        external_busy=_free_busy(request),
    )
    # **In the booking's own transaction**, so a session cannot be committed
    # without whatever venue it was going to get. It no-ops unless the session
    # confirmed — a request that waits for the mentor gets its link at
    # `/accept` — and that guard is inside `provision_meeting` rather than here,
    # because this call site and the transition one would both have to remember
    # it.
    await provision_meeting(session, session_id, rooms=_rooms(request), calendar=_calendar(request))
    row = await get_session_row(session, session_id, user["id"])
    if row is None:  # pragma: no cover - the row was just written in this transaction
        raise NotFoundError("no such session")

    body = SessionRead.from_row(row).model_dump(mode="json")
    await record_response(session, reservation, status_code=CREATED, body=body)
    await session.commit()
    return body, CREATED, False


BookedSessionDep = Annotated[tuple[dict[str, Any], int, bool], Depends(booked_session)]

AdminCreditGrantDep = Annotated[tuple[dict[str, Any], int, bool], Depends(granted_admin_credits)]


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


def _scheduler(request: Request) -> Any:
    """The real scheduler when a token is configured, and a loud nothing else.

    Built per call rather than wired at startup, following `_rooms`: a token
    added to the environment takes effect on the next deploy without a wiring
    change, and a test that sets none gets the null adapter without unsetting
    anything.

    **Read off `app.state` first**, which `_rooms`, `_calendar` and `_free_busy`
    all do and this did not. The inconsistency was invisible until something
    needed to assert *that a reminder was published* rather than what happened
    when one was: there was no seam to put a fake in.
    """
    wired = getattr(request.app.state, "scheduler", None)
    if wired is not None:
        return wired
    settings = _configured(request)
    token = settings.qstash_token
    return (
        QStashScheduler(token.get_secret_value(), settings.qstash_url) if token else NullScheduler()
    )


def _reminder_callback_url(request: Request) -> str | None:
    """Where QStash should call back, or ``None`` if we cannot say.

    **Stated in configuration rather than derived from the request.** A service
    behind a proxy cannot see the URL the caller used, and the signature names
    its destination — so a derived value that is wrong rejects every callback
    with a message about signatures rather than about configuration, which is
    the hardest kind of misconfiguration to diagnose.
    """
    base = _configured(request).public_base_url
    return f"{base.rstrip('/')}{REMINDER_CALLBACK_PATH}" if base else None


async def reminder_callback(request: Request, session: SessionDep) -> bool:
    """Verify the caller is QStash, then fire the reminder if it is still owed.

    **The signature is the whole authorization**, so it is checked before the
    body is parsed as anything — a payload that has not been proved authentic is
    input, not instruction.

    Raises :class:`AuthenticationError` rather than a bespoke status, so this
    endpoint answers `401` through the same handler as everything else. A caller
    who cannot prove who they are has not been *refused permission*; there is
    nobody to refuse.
    """
    settings = _configured(request)
    keys = tuple(
        key.get_secret_value()
        for key in (settings.qstash_current_signing_key, settings.qstash_next_signing_key)
        if key is not None
    )
    url = _reminder_callback_url(request)
    token = request.headers.get("Upstash-Signature", "")
    if not keys or not url:
        # **Refused rather than waved through.** An unconfigured verifier on a
        # public endpoint that queues messages is worse than one that rejects
        # everything: the second is visibly broken, the first is quietly open.
        raise AuthenticationError("callback verification is not configured")

    body = await request.body()
    try:
        verify_callback(token=token, body=body, url=url, signing_keys=keys)
    except UntrustedCallbackError as exc:
        raise AuthenticationError(str(exc)) from exc

    payload = json.loads(body or b"{}")
    session_id = payload.get("session_id")
    kind = payload.get("kind")
    if not session_id or (kind not in REMINDER_OFFSETS and kind not in SESSION_REMINDER_KINDS):
        raise ValidationError("not a reminder callback")

    # **Two kinds of reminder, one callback.** They differ in what they
    # require: a response reminder is only sent while the request is still
    # unanswered, a session reminder only while the session is still going
    # ahead. Dispatching on the kind keeps that in one place rather than in two
    # endpoints that would drift.
    # **Two kinds, one callback.** They differ in what they require: a response
    # reminder is only sent while the request is still unanswered, a session
    # reminder only while the session is still going ahead. Dispatching on the
    # kind keeps that in one place rather than in two endpoints that would drift.
    #
    # **The review reminder is deliberately not here.** It is a sweep, not a
    # scheduled callback — see `remind_unreviewed`, and the measurement that put
    # it there.
    if kind in SESSION_REMINDER_KINDS:
        queued = await remind_before_session(session, UUID(str(session_id)), str(kind))
    else:
        queued = await remind_if_still_waiting(session, UUID(str(session_id)), str(kind))
    await session.commit()
    return queued


ReminderCallbackDep = Annotated[bool, Depends(reminder_callback)]


class RuntimeJobRequest(BaseModel):
    """The complete body QStash sends to a recurring job endpoint."""

    model_config = ConfigDict(extra="forbid", strict=True)

    job_id: str


async def runtime_job_delivery(job_name: str, request: Request) -> dict[str, Any]:
    """Verify raw QStash bytes, then parse and call the shared job runner."""
    settings = _configured(request)
    keys = tuple(
        key.get_secret_value()
        for key in (settings.qstash_current_signing_key, settings.qstash_next_signing_key)
        if key is not None
    )
    if not keys or not settings.public_base_url:
        raise AuthenticationError("runtime callback verification is not configured")

    path = f"/api/v1/internal/jobs/{job_name}"
    destination = f"{settings.public_base_url.rstrip('/')}{path}"
    body = await request.body()
    try:
        verify_callback(
            token=request.headers.get("Upstash-Signature", ""),
            body=body,
            url=destination,
            signing_keys=keys,
        )
    except UntrustedCallbackError as exc:
        raise AuthenticationError(str(exc)) from exc

    if job_name not in RUNTIME_JOB_NAMES:
        raise ValidationError("unknown runtime job")
    try:
        payload = RuntimeJobRequest.model_validate_json(body)
    except PydanticValidationError as exc:
        raise ValidationError("not a runtime job callback") from exc
    if payload.job_id != schedule_id(settings.environment, job_name):
        raise ValidationError("job_id does not match this environment and endpoint")

    message_id = request.headers.get("Upstash-Message-Id")
    logger.info(
        "runtime job delivery",
        extra={
            "job_name": job_name,
            "job_id": payload.job_id,
            "upstash_message_id": message_id,
        },
    )
    runner = getattr(request.app.state, "runtime_jobs", None) or RuntimeJobs(settings)
    result = await runner.run(job_name, job_id=payload.job_id, message_id=message_id)
    return {
        "job": result.name,
        "job_id": result.job_id,
        "status": result.status,
        "counts": result.counts,
    }


RuntimeJobDep = Annotated[dict[str, Any], Depends(runtime_job_delivery)]
#: Where Google sends a mentor back. One constant, because the value is sent to
#: Google on the consent request **and** again on the exchange, and Google
#: refuses the pair if they differ — two strings that happen to agree would fail
#: only in production, and only for the first mentor to try.
CALENDAR_REDIRECT_PATH = "/api/v1/callbacks/google/calendar"


def _calendar_oauth(request: Request) -> tuple[str, str, str, str]:
    """The client, the secret, the redirect and the sealing key, or a refusal.

    All four or none. A consent flow missing any one of them fails somewhere
    downstream with a message about whichever piece it happened to reach first,
    and an operator then debugs the symptom.
    """
    settings = _configured(request)
    base = settings.public_base_url
    if not (
        settings.google_calendar_client_id
        and settings.google_calendar_client_secret
        and settings.calendar_token_key
        and base
    ):
        raise ConfigurationError("calendar connection is not configured")
    return (
        settings.google_calendar_client_id,
        settings.google_calendar_client_secret.get_secret_value(),
        f"{base.rstrip('/')}{CALENDAR_REDIRECT_PATH}",
        settings.calendar_token_key.get_secret_value(),
    )


def _free_busy(request: Request) -> Any:
    """The mentor's external calendar, or a reader that subtracts nothing.

    **Null unless all three settings are present**, which is the same shape
    `_calendar` uses and for the same reason: a deployment part-way through
    being configured should behave like one that has not started, not fail every
    slot render with an OAuth error. Unconnected mentors are unaffected either
    way — the reader checks for a grant before it calls anything.

    Read off `app.state` first so a test can wire a fake, following `_rooms`.
    """
    wired = getattr(request.app.state, "free_busy", None)
    if wired is not None:
        return wired
    settings = _configured(request)
    if not (
        settings.google_calendar_client_id
        and settings.google_calendar_client_secret
        and settings.calendar_token_key
    ):
        return NullFreeBusy()
    return MentorFreeBusy(
        client_id=settings.google_calendar_client_id,
        client_secret=settings.google_calendar_client_secret.get_secret_value(),
        key=settings.calendar_token_key.get_secret_value(),
        # **Its own session for the one write it makes.** A dead grant has to be
        # recorded whether the surrounding read commits or the surrounding
        # booking rolls back, and it must never commit either of them.
        session_factory=getattr(request.app.state, "session_factory", None)
        or get_session_factory(),
    )


def _token_exchange(request: Request) -> Any:
    """The call that turns a consent code into a refresh token.

    **Read off `app.state` rather than called directly**, following `_rooms` and
    `_calendar`. The same reason applies with more force here: this is the one
    outbound call in the codebase whose *request* is the thing that has to be
    right — `access_type`, `prompt` and a redirect that matches the consent
    byte-for-byte — and a seam is what lets a test assert what was asked rather
    than what came back.
    """
    return getattr(request.app.state, "calendar_exchange", None) or exchange_code


async def calendar_consent_url(request: Request, user: CurrentUserDep) -> str:
    """Where to send this mentor to grant free/busy access.

    **The `state` carries the mentor and is sealed**, which is the whole CSRF
    control here: without it, an attacker could complete their *own* Google
    consent against a victim's session and attach their calendar to somebody
    else's account. The seal makes the mentor's id unforgeable and its ten-minute
    TTL makes a captured URL useless by the time anybody finds it.
    """
    client_id, _, redirect_uri, key = _calendar_oauth(request)
    state = sealed_value({"user_id": str(user["id"])}, key=key)
    return consent_url(client_id=client_id, redirect_uri=redirect_uri, state=state)


async def calendar_connected(
    request: Request, session: SessionDep, code: str = "", state: str = ""
) -> UUID:
    """Complete the grant Google is redirecting back from.

    **No `CurrentUserDep`, and that is the point of the sealed state.** Google
    redirects a browser here; there is no bearer token on that request, so the
    mentor's identity has to travel in the `state` we issued — which is exactly
    why it is sealed rather than merely passed.

    Raises :class:`AuthenticationError` for a state we did not issue, so a
    forged callback answers the same way an unauthenticated request does.
    """
    client_id, client_secret, redirect_uri, key = _calendar_oauth(request)
    if not code or not state:
        raise ValidationError("this is not a completed consent")

    try:
        opened = unsealed_value(state, key=key)
    except SealError as exc:
        raise AuthenticationError(str(exc)) from exc
    user_id = UUID(str(opened["user_id"]))

    tokens = _token_exchange(request)(
        code=code,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
    )
    # **Checked here as well as in the adapter**, which is not belt-and-braces:
    # the adapter is swappable through `app.state`, and the failure this guards
    # against is a `KeyError` reaching the client as a 500 rather than as the
    # refusal it is. Indexing a dict that came from outside is the mistake.
    refresh_token = str(tokens.get("refresh_token") or "")
    if not refresh_token:
        raise VenueUnavailableError("google returned no refresh token")

    await connect(
        session,
        user_id,
        # **Nothing here names the Google account.** It would come from an
        # `id_token`, and Google issues one only when `openid` is among the
        # scopes — ADR 0012 asks for `calendar.freebusy` alone, so
        # `external_account_id` stays null rather than the consent screen
        # growing a second line to fill it.
        refresh_token_encrypted=seal(refresh_token, key=key),
    )
    await session.commit()
    return user_id


async def own_calendar(user: CurrentUserDep, session: SessionDep) -> dict[str, Any] | None:
    """This mentor's live grant, or ``None`` if they have not connected."""
    return await active_connection(session, user["id"])


async def disconnected_calendar(user: CurrentUserDep, session: SessionDep) -> bool:
    """Revoke this mentor's grant. ``False`` if they had none.

    The route turns that into a `404` rather than an idempotent `204`: a mentor
    who thinks they disconnected something needs to know they did not, and the
    thing they would be wrong about is whether a credential still exists.
    """
    removed = await disconnect(session, user["id"])
    await session.commit()
    return removed


CalendarConsentDep = Annotated[str, Depends(calendar_consent_url)]
CalendarConnectedDep = Annotated[UUID, Depends(calendar_connected)]
OwnCalendarDep = Annotated[dict[str, Any] | None, Depends(own_calendar)]
DisconnectedCalendarDep = Annotated[bool, Depends(disconnected_calendar)]


def transitions(action: str) -> Callable[..., Awaitable[None]]:
    """Build the dependency for one named transition.

    **A factory rather than four copies**, and the argument is safe in a way the
    `include_inactive` flag `session_type_is_live` refused was not: `action` is
    a literal fixed at each of the four call sites below, never a value a caller
    can send. A mis-defaulted flag there would have made a deactivated offering
    bookable; there is no default here to get wrong.

    Every rule the action carries — who may take it, from which state, which
    reason codes they may give — is looked up in `domain/sessions.py` by this
    name, so the four differ by a table row rather than by a code path.
    """

    async def run(
        session_id: UUID,
        user: dict[str, Any],
        session: AsyncSession,
        request: Request,
        payload: SessionTransitionWrite | None,
    ) -> None:
        await transition(
            session,
            session_id,
            user["id"],
            action,
            payload.model_dump() if payload else {},
            now=dt.datetime.now(dt.UTC),
        )
        # The second confirmation point. Accepting is the moment a
        # confirmation-required session becomes real, and it is the only action
        # that produces `confirmed` — declining, withdrawing and cancelling all
        # end a session rather than starting one.
        if action == "accept":
            await provision_meeting(
                session, session_id, rooms=_rooms(request), calendar=_calendar(request)
            )
            # **The second place a session becomes real**, and therefore the
            # second place its reminders are published. Booking covers the
            # offering that confirms itself; this covers the one that waited.
            # Missing it would leave every confirmation-required session
            # silently unreminded — which is exactly the shape of gap that made
            # `release_meeting` necessary.
            row = await get_session_row(session, session_id, user["id"])
            if row is not None:
                schedule_session_reminders(
                    session_id,
                    row["starts_at"],
                    scheduler=_scheduler(request),
                    callback_url=_reminder_callback_url(request) or "",
                    now=dt.datetime.now(dt.UTC),
                )
        else:
            # **Every other transition ends the session**, and an ended session
            # must not leave a live event in either calendar. Decline, withdraw
            # and cancel are the three; `accept` is the only one that creates
            # rather than releases.
            await release_meeting(session, session_id, calendar=_calendar(request))
        await session.commit()

    # **Two signatures over one body**, because the annotation is the contract.
    # FastAPI reads it to build the request schema, so cancelling can only ask
    # its extra question by being annotated differently — and `accept` must not
    # inherit a field it would have to ignore. The body stays in one place; only
    # the shape the client sends differs.
    async def cancelling(
        session_id: UUID,
        user: CurrentUserDep,
        session: SessionDep,
        request: Request,
        payload: SessionCancellationWrite | None = None,
    ) -> None:
        await run(session_id, user, session, request, payload)

    async def ending(
        session_id: UUID,
        user: CurrentUserDep,
        session: SessionDep,
        request: Request,
        payload: SessionTransitionWrite | None = None,
    ) -> None:
        await run(session_id, user, session, request, payload)

    return cancelling if action == "cancel" else ending


AcceptedSessionDep = Annotated[None, Depends(transitions("accept"))]
DeclinedSessionDep = Annotated[None, Depends(transitions("decline"))]
WithdrawnSessionDep = Annotated[None, Depends(transitions("withdraw"))]
CancelledSessionDep = Annotated[None, Depends(transitions("cancel"))]


async def joined_session(
    session_id: UUID, user: CurrentUserDep, session: SessionDep, request: Request
) -> str | None:
    """Record that the caller arrived, and hand back the door.

    **Not a transition, and not in the table above.** Arriving changes no
    status: a session stays `confirmed` while it runs, and what it becomes is
    decided once for both parties when the join window shuts. Putting this in
    `TRANSITIONS` would have needed a `to` state it does not have.

    **The URL is minted here rather than stored**, and only for a private room.
    A Daily room refuses anybody without a token, so recording an arrival and
    returning nothing would close none of the gap this endpoint exists for —
    and storing the token instead would put two live bearer credentials per
    session into the database and every backup, outliving the session they open.

    For every other venue the door is the stored URL: a Meet link is on the
    calendar event, and a custom venue is the address the mentor typed.
    """
    now = dt.datetime.now(dt.UTC)
    row = await record_arrival(session, session_id, user["id"], now=now)
    await session.commit()

    stored = row["meeting_url"]
    if row["meeting_provider"] != MeetingProvider.DAILY or not row["external_room_id"]:
        return str(stored) if stored else None

    # Only the opening edge: the token outlives the join window for the same
    # reason the room does — one expiring at `join_closes_at` would evict its
    # holder fifteen minutes into an hour-long session.
    opens, _ = join_window(row["starts_at"])
    try:
        token = _rooms(request).token_for(
            room=str(row["external_room_id"]),
            user_id=str(user["id"]),
            user_name=str(user.get("first_name") or "Guest"),
            # The mentor hosts: on Daily an owner may admit, mute and end.
            is_owner=bool(row["is_mentor"]),
            opens_at=opens,
            # The room outlives the window by the session's length, and so must
            # the token — one expiring at `join_closes_at` would evict its
            # holder fifteen minutes into an hour.
            closes_at=row["starts_at"] + dt.timedelta(minutes=int(row["duration_minutes"])),
        )
    except (VenueUnavailableError, NotImplementedError) as exc:
        # **Not a failure of the join.** The arrival is recorded and committed;
        # what is missing is a way in, and saying so honestly beats a 500 on a
        # request that already did the thing it was asked to do.
        logger.info("no door for session %s: %s", session_id, exc)
        return None

    return f"{stored}?t={token}"


JoinedSessionDep = Annotated[str | None, Depends(joined_session)]


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
    request: Request,
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
        external_busy=_free_busy(request),
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
        "reviews": await mentor_review_stats(session, user_id),
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


async def deleted_own_session_type(
    session_type_id: UUID, user: CurrentUserDep, session: SessionDep
) -> bool:
    """Soft-delete, or a `409` raised from the store when sessions are booked.

    The refusal is raised rather than returned because it is not the absence of a
    row: `False` already means *not yours or already gone*, and folding a second
    meaning into one boolean is how a 409 becomes a 404 at the route.
    """
    removed = await delete_session_type(session, user["id"], session_type_id)
    await session.commit()
    return removed


CreatedOwnSessionTypeDep = Annotated[UUID, Depends(created_own_session_type)]
UpdatedOwnSessionTypeDep = Annotated[bool, Depends(updated_own_session_type)]
DeletedOwnSessionTypeDep = Annotated[bool, Depends(deleted_own_session_type)]


async def own_questions(
    session_type_id: UUID, user: CurrentUserDep, session: SessionDep
) -> list[dict[str, Any]]:
    """This offering's live questions, or a 404 that says nothing about why.

    `None` from the store means the offering is not the caller's — or does not
    exist, or is deleted. Indistinguishable on purpose: telling them apart says
    which ids exist.
    """
    rows = await list_questions(session, user["id"], session_type_id)
    if rows is None:
        raise NotFoundError("no such session type")
    return rows


async def created_own_question(
    session_type_id: UUID, payload: QuestionWrite, user: CurrentUserDep, session: SessionDep
) -> UUID:
    question_id = await create_question(session, user["id"], session_type_id, payload.model_dump())
    if question_id is None:
        raise NotFoundError("no such session type")
    await session.commit()
    return question_id


async def updated_own_question(
    session_type_id: UUID,
    question_id: UUID,
    payload: QuestionPatch,
    user: CurrentUserDep,
    session: SessionDep,
) -> bool:
    changed = await update_question(
        session,
        user["id"],
        session_type_id,
        question_id,
        payload.model_dump(exclude_unset=True),
    )
    await session.commit()
    return changed


async def deleted_own_question(
    session_type_id: UUID, question_id: UUID, user: CurrentUserDep, session: SessionDep
) -> bool:
    removed = await delete_question(session, user["id"], session_type_id, question_id)
    await session.commit()
    return removed


OwnQuestionsDep = Annotated[list[dict[str, Any]], Depends(own_questions)]
CreatedOwnQuestionDep = Annotated[UUID, Depends(created_own_question)]
UpdatedOwnQuestionDep = Annotated[bool, Depends(updated_own_question)]
DeletedOwnQuestionDep = Annotated[bool, Depends(deleted_own_question)]

SessionsPageDep = Annotated[tuple[list[dict[str, Any]], bool], Depends(target_sessions)]
SessionDetailDep = Annotated[dict[str, Any], Depends(viewer_session)]
SessionEventsDep = Annotated[list[dict[str, Any]], Depends(viewer_session_events)]


# --------------------------------------------------------------------------
# Reviews
#
# `now` is taken once per request and threaded through, rather than each layer
# calling the clock. Two reads of `datetime.now()` inside one write can land
# either side of a window's edge, and the test that pins the boundary needs a
# clock it can set.
# --------------------------------------------------------------------------


async def written_review(
    payload: ReviewWrite, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    """Write the review and read it back, in one transaction.

    Read back through a `SELECT` rather than assembled from the insert's own
    values, following `booked_session`: the response is the shape every other
    review read returns, built by the same code, so the two cannot drift.
    """
    review_id = await write_review(
        session, user["id"], payload.to_columns(), now=dt.datetime.now(dt.UTC)
    )
    row = await get_review_row(session, review_id, user["id"])
    if row is None:  # pragma: no cover - written in this transaction
        raise NotFoundError("no such review of yours")
    await session.commit()
    return row


async def edited_review(
    review_id: UUID, payload: ReviewEdit, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    """Apply the edit and return the review as it now stands."""
    now = dt.datetime.now(dt.UTC)
    await edit_review(session, user["id"], review_id, payload.to_columns(), now=now)
    row = await get_review_row(session, review_id, user["id"])
    if row is None:  # pragma: no cover - `edit_review` already refused if absent
        raise NotFoundError("no such review of yours")
    await session.commit()
    return row


async def own_reviewable_sessions(
    user: CurrentUserDep,
    session: SessionDep,
    mentor_id: Annotated[
        UUID | None,
        Query(description="Narrow to one mentor's sessions, which is what a profile tab wants."),
    ] = None,
    limit: Annotated[int | None, Query(ge=1, le=MAX_PAGE_SIZE)] = None,
) -> list[dict[str, Any]]:
    """What the caller may review right now, newest first."""
    result = await session.execute(
        reviewable_sessions(
            user["id"], dt.datetime.now(dt.UTC), mentor_id, limit=clamp_limit(limit)
        )
    )
    return [dict(row) for row in result.mappings()]


WrittenReviewDep = Annotated[dict[str, Any], Depends(written_review)]
EditedReviewDep = Annotated[dict[str, Any], Depends(edited_review)]
ReviewableSessionsDep = Annotated[list[dict[str, Any]], Depends(own_reviewable_sessions)]


async def mentor_reviews_page(
    handle: str,
    session: SessionDep,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int | None, Query(ge=1, le=MAX_PAGE_SIZE)] = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """One page of a mentor's published reviews.

    The handle resolves through the same predicate and the same visibility pair
    the profile uses, so a paused mentor's reviews are absent exactly as their
    profile is. `404` rather than an empty page: no such mentor is a different
    answer from a mentor with nothing to show, and a client renders them
    differently.
    """
    mentor = await get_public_mentor_id(session, handle)
    if mentor is None:
        raise NotFoundError("no such mentor")
    # The **two-part** codec, because the list sorts on `created_at` rather than
    # on the id. Mispairing the two forms is a paging bug that only shows on page
    # two, which this endpoint has already had once.
    rows, has_more = await list_mentor_reviews(
        session, mentor, limit=clamp_limit(limit), after=decode_cursor(cursor)
    )
    # **Minted here, beside the decode.** `mentor_page` states the rule: the token
    # is issued where the sort key is known, because deriving it again in the
    # route is one rule in two places — and the two halves drifting apart is
    # exactly the defect that made every cursor this endpoint issued invalid.
    if not (has_more and rows):
        return rows, None
    last = rows[-1]
    return rows, encode_cursor(last["created_at"].isoformat(), last["id"])


MentorReviewsDep = Annotated[tuple[list[dict[str, Any]], str | None], Depends(mentor_reviews_page)]


async def own_reviews_page(
    user: CurrentUserDep,
    session: SessionDep,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int | None, Query(ge=1, le=MAX_PAGE_SIZE)] = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """One page of the reviews written *about* the caller.

    No authorization argument and no handle: `CurrentUserDep` is the subject, so
    there is no target to check and nothing a caller could name that is not
    theirs.

    The same two-part codec the public list uses, minted here beside the decode
    for the reason `mentor_page` records — deriving the token again in the route
    is one rule in two places, and the two halves drifting is what once made
    every cursor that endpoint issued invalid.
    """
    rows, has_more = await list_reviews_about(
        session, user["id"], limit=clamp_limit(limit), after=decode_cursor(cursor)
    )
    if not (has_more and rows):
        return rows, None
    last = rows[-1]
    return rows, encode_cursor(last["created_at"].isoformat(), last["id"])


OwnReviewsDep = Annotated[tuple[list[dict[str, Any]], str | None], Depends(own_reviews_page)]


async def reported_review(
    review_id: UUID,
    payload: ReviewReportWrite,
    user: CurrentUserDep,
    session: SessionDep,
) -> Any:
    """File a report against a review of the caller.

    The composite key is the guarantee; `report_review` scopes the lookup so a
    review about somebody else is **404 rather than 403** — confirming it exists
    would turn an authorization answer into an enumeration oracle.
    """
    filed = await report_review(
        session,
        user["id"],
        review_id,
        reason=payload.reason,
        detail=payload.detail,
    )
    await session.commit()
    return filed


ReportedReviewDep = Annotated[Any, Depends(reported_review)]


async def moderation_queue_page(
    _: QueueViewerDep,
    session: SessionDep,
    reported: Annotated[bool, Query()] = False,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int | None, Query(ge=1, le=MAX_PAGE_SIZE)] = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """One page of reviews for a moderator.

    `QueueViewerDep` rather than the acting grant: every live admin may look,
    which is the split `pending_institution_rows` and the mentor queue already
    use. Deciding is narrower — see below.
    """
    rows, has_more = await list_reviews_for_moderation(
        session,
        limit=clamp_limit(limit),
        after=decode_cursor(cursor),
        reported_only=reported,
    )
    if not (has_more and rows):
        return rows, None
    last = rows[-1]
    return rows, encode_cursor(last["created_at"].isoformat(), last["id"])


ModerationQueueDep = Annotated[
    tuple[list[dict[str, Any]], str | None], Depends(moderation_queue_page)
]


async def decided_report(
    report_id: UUID,
    payload: ReportDecisionWrite,
    admin_id: CatalogueAdminDep,
    session: SessionDep,
) -> Any:
    """Rule on a report, and remove the review if it is upheld.

    **`CatalogueAdminDep` — super_admin only.** Every live grant may *look* at
    the queue; removing somebody's review from a public profile is the same
    weight as curating the catalogue, and `AdminRole` has no moderation grant to
    name. Adding one without anything to grant it would make the enum
    decorative, which settled decision #21 refuses.
    """
    decision = await decide_report(session, admin_id, report_id, payload.outcome)
    await session.commit()
    return decision


DecidedReportDep = Annotated[Any, Depends(decided_report)]
