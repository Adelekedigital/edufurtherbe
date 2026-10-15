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

from app.api.deps import (
    ClaimedReferralDep,
    CreatedReferralDep,
    OwnInviteLinkDep,
    OwnReferralsDep,
    PickedInviteHandleDep,
)
from app.api.schemas.referrals import InviteLinkRead, ReferralClaimRead, ReferralRead

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
        "Attaches the caller to the invite carrying this code: a single-use "
        "invite's code, or somebody's personal link (`POST /me/invite-link`). "
        "Single-use codes are looked up first, then current link codes; a "
        "link's retired code is `404` like any unknown one.\n\n"
        "**A personal link is reusable.** Each arrival through it becomes its "
        "own invite of the link's owner, with a code of its own, so the "
        "response's `code` is that new invite's, not the link's. One invite per "
        "arrival still holds: claiming a link after another person's invite is "
        "`409`, and after an invite from the same person returns that invite.\n\n"
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
            "description": (
                "The invite or link is your own, somebody else has claimed the "
                "invite, or you already arrived through somebody else's."
            )
        },
    },
)
async def claim_own_referral(referral: ClaimedReferralDep) -> ReferralClaimRead:
    """No 201: this creates nothing. It attaches the caller to a row that
    already existed, which is an update however it reads."""
    return ReferralClaimRead.model_validate(referral)


LINK_PATH = "/api/v1/me/invite-link"


@router.post(
    "/invite-link",
    response_model=InviteLinkRead,
    status_code=status.HTTP_201_CREATED,
    summary="Your personal invite link",
    description=(
        "Returns your one permanent invite code, creating it on the first call "
        "(`201` with `Location`); every later call returns the same code with "
        "`200`. Two first calls at once agree on one code.\n\n"
        "Anyone may sign up through it, and each arrival becomes its own invite "
        "with the same reward rules as any other: your two credits arrive once, "
        "when the first person you invited finishes their profile.\n\n"
        "The code is random until you pick a handle (`PUT`). The client builds "
        "the link itself, as `/invite/{code}`."
    ),
    responses={
        **UNAUTHORISED,
        status.HTTP_200_OK: {
            "description": "You already had a link; this is its code.",
            "model": InviteLinkRead,
        },
    },
)
async def own_invite_link(link: OwnInviteLinkDep, response: Response) -> InviteLinkRead:
    code, created = link
    if created:
        response.headers["Location"] = LINK_PATH
    else:
        response.status_code = status.HTTP_200_OK
    return InviteLinkRead(code=code)


@router.put(
    "/invite-link",
    response_model=InviteLinkRead,
    summary="Pick a handle for your invite link",
    description=(
        "Sets your link's code to a handle of your choice, creating the link if "
        "you have none. The handle is lowercased, then must be 3 to 30 "
        "characters of letters, digits and single hyphens, not at either end, "
        "and not a reserved word (`422` otherwise).\n\n"
        "**Your previous code stops working at once**: a link using it now "
        "lands on the page for an unknown invite. It is held for 30 days, in "
        "which nobody else may pick it but you may take it back; after that "
        "anyone may. Picking the handle you already have changes nothing.\n\n"
        "`409` when the handle is somebody's current code, a single-use "
        "invite's code, or somebody else's previous code inside its 30 days."
    ),
    responses={
        **UNAUTHORISED,
        status.HTTP_409_CONFLICT: {"description": "That handle is taken."},
    },
)
async def pick_invite_handle(code: PickedInviteHandleDep) -> InviteLinkRead:
    return InviteLinkRead(code=code)
