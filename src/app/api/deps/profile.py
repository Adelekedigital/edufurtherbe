"""A user's own profile, attributes, referrals, onboarding and images."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, File, Path, Query, Request, UploadFile
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from app.api.deps.core import (
    BookingWindowDep,
    CurrentUserDep,
    LadderDep,
    OwnerDep,
    SessionDep,
    TargetUserDep,
    get_storage,
    refuse_window_out_of_range,
    window_out_of_range,
)
from app.api.schemas.common import (
    LOOKUP_PAGE_SIZE,
    MAX_PAGE_SIZE,
    StorableText,
    clamp_limit,
    decode_cursor,
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
from app.api.schemas.referrals import ReferralClaim, ReferralWrite
from app.core.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.domain.assets import AssetKind
from app.domain.images import MAX_UPLOAD_BYTES
from app.infra.db.asset_store import clear_banner, store_image, stored_avatar_focus
from app.infra.db.catalogue_store import LOOKUPS, list_lookup, search_institutions
from app.infra.db.credit_store import get_credit_summary
from app.infra.db.education_writer import create_education, delete_education, update_education
from app.infra.db.onboarding_store import get_onboarding
from app.infra.db.onboarding_writer import OnboardingResult, complete_onboarding
from app.infra.db.profile_store import (
    get_goal,
    get_mentor_profile,
    list_awards,
    list_education,
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
from app.infra.db.session_stats import mentee_completed_sessions

# `get_session` is aliased: this module already has one, and it is the **database
# session** dependency at line 142. Two callables with that name in one file is a
# collision a reader resolves by scrolling, and the wrong one is a plausible
# mistake rather than an obvious error — `bubble_id` shadowed a local the same
# way in the M4 transform and raised `UnboundLocalError` far from the edit.
from app.infra.storage.supabase import SupabaseStorage

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
        "mentee_completed_sessions": await mentee_completed_sessions(session, user_id),
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
    payload: MentorProfileWrite, user_id: OwnerDep, session: SessionDep, window: BookingWindowDep
) -> UUID:
    """A second application is a 409, not a second row.

    `uq_mentor_profiles_user_id` is what actually prevents the duplicate; this
    checks first so the caller gets a considered answer rather than a constraint
    violation surfacing as a 500.
    """
    refuse_window_out_of_range(payload.booking_window_days, window)
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
    payload: MentorProfileWrite, user_id: OwnerDep, session: SessionDep, window: BookingWindowDep
) -> bool:
    stored = None
    if window_out_of_range(payload.booking_window_days, window):
        current = await get_mentor_profile(session, user_id)
        stored = current["booking_window_days"] if current else None
    refuse_window_out_of_range(payload.booking_window_days, window, stored=stored)
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


async def replaced_languages(
    payload: UserLanguagesWrite, user_id: OwnerDep, session: SessionDep
) -> None:
    await replace_languages(
        session, user_id, [entry.model_dump(exclude_unset=True) for entry in payload.languages]
    )
    await session.commit()


ReplacedLanguagesDep = Annotated[None, Depends(replaced_languages)]


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
    storage: SupabaseStorage = getattr(request.app.state, "storage", None) or get_storage()
    url, previous = await store_image(session, storage, kind, user_id, payload)
    await session.commit()

    # **After the commit, and never fatal** — `drop_url`'s contract.
    if previous and previous != url:
        await run_in_threadpool(storage.drop_url, previous)

    return url


async def uploaded_avatar(
    request: Request,
    user_id: OwnerDep,
    session: SessionDep,
    file: Annotated[UploadFile, File(description="JPEG, PNG or WebP, up to 5 MB.")],
) -> tuple[str, tuple[object, object]]:
    """The stored avatar's URL, and its focus as stored after the upload."""
    url = await _store_image(AssetKind.AVATAR, file, user_id, session, request)
    return url, await stored_avatar_focus(session, user_id)


async def uploaded_banner(
    request: Request,
    user_id: OwnerDep,
    session: SessionDep,
    file: Annotated[UploadFile, File(description="JPEG, PNG or WebP, up to 5 MB.")],
) -> str:
    return await _store_image(AssetKind.BANNER, file, user_id, session, request)


async def removed_banner(request: Request, user_id: OwnerDep, session: SessionDep) -> None:
    """Unset the owner's banner, then delete its object. Idempotent.

    Storage is resolved **before** the write, as the upload does: an app with no
    storage configured refuses up front rather than clearing the banner and then
    failing after the commit.
    """
    storage: SupabaseStorage = getattr(request.app.state, "storage", None) or get_storage()
    previous = await clear_banner(session, user_id)
    await session.commit()
    if previous is not None:
        await run_in_threadpool(storage.drop_url, previous)


UploadedAvatarDep = Annotated[tuple[str, tuple[object, object]], Depends(uploaded_avatar)]
UploadedBannerDep = Annotated[str, Depends(uploaded_banner)]
RemovedBannerDep = Annotated[None, Depends(removed_banner)]
