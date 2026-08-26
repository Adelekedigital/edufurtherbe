"""Invites: sending them, listing them, and claiming one you arrived with.

**A fifth router on ``/api/v1/me``**, for the reason `me_session_types` records:
the prefix is shared, the subject is not. This is the referral programme, which
is a domain of its own — settled decision #64 gives everything outside the
catalogue its domain name.

**There is no invite UI yet**, and the endpoints ship regardless. M5a shipped
the same way: a surface the front end has not caught up with is unreachable, not
wrong, and building it after the screens exist means the screens have nothing to
build against.
"""

from fastapi import APIRouter, Response, status

from app.api.deps import ClaimedReferralDep, CreatedReferralDep, OwnReferralsDep
from app.api.schemas.referrals import ReferralClaimRead, ReferralRead

router = APIRouter(prefix="/api/v1/me", tags=["referrals"])

UNAUTHORISED: dict[int | str, dict[str, str]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "The bearer token is absent, malformed, expired or wrongly signed."
    }
}


@router.post(
    "/referrals",
    response_model=ReferralRead,
    status_code=status.HTTP_201_CREATED,
    summary="Invite somebody",
    description=(
        "Creates an invite and returns its code.\n\n"
        "`invitee_email` is optional — a shared link has no addressee, and the "
        "code is what attributes an arrival either way. Inviting the same "
        "address twice is refused with `409`; two *different* referrers may "
        "invite the same person, because scoping uniqueness to the address "
        "alone would make the programme a race to invite.\n\n"
        "**The invite pays nothing on its own.** The referrer's two credits "
        "arrive when the invitee finishes their profile, which is the same "
        "signal as the starter credit — signing up and vanishing unlocks "
        "nothing, and that separation is the abuse boundary."
    ),
    responses={
        **UNAUTHORISED,
        status.HTTP_409_CONFLICT: {"description": "You have already invited this address."},
    },
)
async def create_own_referral(referral: CreatedReferralDep, response: Response) -> ReferralRead:
    response.headers["Location"] = "/api/v1/me/referrals"
    return ReferralRead.model_validate(referral)


@router.get(
    "/referrals",
    response_model=list[ReferralRead],
    summary="Invites you have sent",
    description=(
        "Newest first. `signed_up_at` is set when the invitee claims their "
        "code, and `qualified_at` when they finish onboarding — the second is "
        "what opens your recurring grant.\n\n"
        "There is no `status` field: it is derivable from those two timestamps, "
        "and a computed one here would put the same rule in a third place."
    ),
    responses=UNAUTHORISED,
)
async def list_own_referrals(referrals: OwnReferralsDep) -> list[ReferralRead]:
    return [ReferralRead.model_validate(row) for row in referrals]


@router.post(
    "/referrals/claim",
    response_model=ReferralClaimRead,
    summary="Claim an invite you arrived with",
    description=(
        "Attaches the caller to the invite carrying this code.\n\n"
        "**Called by the invitee, after they sign in.** This service never sees "
        "a signup — accounts are created by the provisioning CLI and the "
        "migration — so the front end holds the code from the invite link "
        "across sign-up and presents it here.\n\n"
        "Idempotent: claiming a code you already hold returns it unchanged, "
        "because a retried request must not read as an error. Refused with "
        "`409` for your own invite or one somebody else has already claimed, "
        "and `404` when no invite carries the code."
    ),
    responses={
        **UNAUTHORISED,
        status.HTTP_404_NOT_FOUND: {"description": "No invite carries that code."},
        status.HTTP_409_CONFLICT: {
            "description": "The invite is your own, or somebody else has claimed it."
        },
    },
)
async def claim_own_referral(referral: ClaimedReferralDep) -> ReferralClaimRead:
    """No 201: this creates nothing. It attaches the caller to a row that
    already existed, which is an update however it reads."""
    return ReferralClaimRead.model_validate(referral)
