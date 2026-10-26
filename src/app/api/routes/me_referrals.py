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
    ToggledInviteLinkDep,
)
from app.api.schemas.referrals import InviteLinkRead, ReferralClaimRead, ReferralRead
from app.core.config import Settings
from app.domain.referrals import (
    DEFAULT_SUFFIX_LENGTH,
    HANDLE_MAX_LENGTH,
    HANDLE_MIN_LENGTH,
    RETIRED_CODE_HOLD,
)

router = APIRouter(prefix="/api/v1/me", tags=["referrals"])

#: The deployment default, read from the setting's own declaration so the
#: published text cannot drift from it.
CLAIM_WINDOW_DEFAULT_HOURS: int = Settings.model_fields["invite_claim_window_hours"].default
#: The window, said once for the description and the 409 alike.
CLAIM_WINDOW_TEXT = (
    "the deployment's claim window, `INVITE_CLAIM_WINDOW_HOURS` hours "
    f"(default {CLAIM_WINDOW_DEFAULT_HOURS} = {CLAIM_WINDOW_DEFAULT_HOURS / 24:g} days)"
)

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
        "nothing, and that separation is the abuse boundary. The three free "
        "sessions a month also wait for the referrer's own first attended "
        "session as a mentee (decision 295)."
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
        "**Called by the invitee, after they sign in.** The front end holds "
        "the code from the invite link across sign-up and presents it here.\n\n"
        f"**Only a new account may claim**: one created within {CLAIM_WINDOW_TEXT}. "
        "An older account, including every "
        "account migrated from the previous platform, is refused with `409` "
        "and type `/problems/invite-for-new-accounts`, and nothing is "
        "attached.\n\n"
        "Idempotent: claiming a code you already hold returns it unchanged, "
        "however old the account is now, because a retried request must not "
        "read as an error. Refused with `409` for your own invite or one "
        "somebody else has already claimed, and `404` when no invite carries "
        "the code. Those answers come before the age check, so an old account "
        "gets them unchanged."
    ),
    responses={
        **UNAUTHORISED,
        status.HTTP_404_NOT_FOUND: {"description": "No invite carries that code."},
        status.HTTP_409_CONFLICT: {
            "description": (
                "The invite or link is your own, somebody else has claimed the "
                "invite, or you already arrived through somebody else's; or, "
                "with type `/problems/invite-for-new-accounts`, your account "
                f"is older than {CLAIM_WINDOW_TEXT}."
            )
        },
    },
)
async def claim_own_referral(referral: ClaimedReferralDep) -> ReferralClaimRead:
    """No 201: this creates nothing. It attaches the caller to a row that
    already existed, which is an update however it reads."""
    return ReferralClaimRead.model_validate(referral)


#: The personal link's path under this router, and the `Location` a create
#: names: one string, so the two cannot drift.
LINK = "/invite-link"
HOLD_DAYS = RETIRED_CODE_HOLD.days


@router.post(
    LINK,
    response_model=InviteLinkRead,
    status_code=status.HTTP_201_CREATED,
    summary="Your personal invite link",
    description=(
        "Returns your one permanent invite link, creating it on the first call "
        "(`201` with `Location`); every later call returns it with `200`, "
        "revoked or not (`active`). Two first calls at once agree on one code.\n\n"
        "A new link's code is your first name and a few random characters, "
        f"such as `damilola-7k2x` ({DEFAULT_SUFFIX_LENGTH} characters after the "
        "hyphen; `friend-…` when the name has no "
        "letters a-z to use).\n\n"
        "Anyone may sign up through it, and each arrival becomes its own invite "
        "with the same reward rules as any other: your two credits arrive once, "
        "when the first person you invited finishes their profile.\n\n"
        "You may pick a handle instead (`PUT`) and revoke or reactivate the "
        "link (`PATCH`). The client builds the link itself, as `/invite/{code}`."
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
    state, created = link
    if created:
        response.headers["Location"] = router.prefix + LINK
    else:
        response.status_code = status.HTTP_200_OK
    return InviteLinkRead(code=state.code, active=state.active)


@router.put(
    LINK,
    response_model=InviteLinkRead,
    summary="Pick a handle for your invite link",
    description=(
        "Sets your link's code to a handle of your choice, creating the link if "
        f"you have none. The handle is lowercased, then must be {HANDLE_MIN_LENGTH} "
        f"to {HANDLE_MAX_LENGTH} characters of letters, digits and single hyphens, "
        "not at either end, and not a reserved word (`422` otherwise). A handle "
        "resolves whatever case it is typed in.\n\n"
        "**Your previous code stops working at once**: a link using it now "
        f"lands on the page for an unknown invite. It is held for {HOLD_DAYS} days, "
        "in which nobody else may pick it but you may take it back; after that "
        "anyone may. Picking the handle you already have changes nothing. "
        "**Renaming a revoked link keeps it revoked.**\n\n"
        "`409` when the handle is somebody's current code, a single-use "
        f"invite's code, or somebody else's previous code inside its {HOLD_DAYS} days."
    ),
    responses={
        **UNAUTHORISED,
        status.HTTP_409_CONFLICT: {"description": "That handle is taken."},
    },
)
async def pick_invite_handle(link: PickedInviteHandleDep) -> InviteLinkRead:
    return InviteLinkRead(code=link.code, active=link.active)


@router.patch(
    LINK,
    response_model=InviteLinkRead,
    summary="Revoke or reactivate your invite link",
    description=(
        '`{"active": false}` revokes your link and `{"active": true}` '
        "reactivates it; both are idempotent and return the link. Creates the "
        "link first if you have none.\n\n"
        "**While revoked**, its code is answered as unknown everywhere: claiming "
        "it is `404` and the invite landing names nobody. It stays yours: "
        "nobody else may pick it, you still read it here, and you may rename it "
        "(it stays revoked). Reactivating makes the same code work again.\n\n"
        "People who already arrived through the link are unaffected, including "
        "the credits their finishing earns you."
    ),
    responses=UNAUTHORISED,
)
async def toggle_invite_link(link: ToggledInviteLinkDep) -> InviteLinkRead:
    return InviteLinkRead(code=link.code, active=link.active)
