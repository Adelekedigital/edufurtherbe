"""The public invite lookup behind the `/invite/{code}` landing page.

**Unauthenticated, and the first read of user data in the referral domain that
is** (decision 256). The landing renders before sign-in, so it takes no token
and reads none: one sent is ignored, valid or not.

**Any code that cannot be joined through is one `200 {"inviter": null}`**:
unknown, malformed, too long, a link's retired code, a revoked link, a claimed
single-use code, or an inviter who was deleted. Same body, same headers, so a
retired or revoked handle cannot be told from one that never existed. Not an
error, at the frontend's request, so its landing page and its server-side link
preview never dead-end (owner, 2026-10-15).
"""

from fastapi import APIRouter, Response

from app.api.deps import InviterDep
from app.api.deps.core import PRIVATE_NO_STORE
from app.api.schemas.referrals import InviteLookupRead, InviterRead

router = APIRouter(prefix="/api/v1/invites", tags=["referrals"])


@router.get(
    # `:path`, so a code carrying `/` still reaches the one answer below rather
    # than the router's own 404.
    "/{code:path}",
    response_model=InviteLookupRead,
    summary="Who an invite is from",
    description=(
        "For the invite landing page, before sign-in. No token needed; one sent "
        "is ignored.\n\n"
        "Resolves an active personal link's current code or a single-use invite "
        "nobody has claimed, and returns the inviter's **first name and photo "
        "only**, either of which may be `null`.\n\n"
        'Everything else is `200` with `{"inviter": null}`, one body for all: '
        "unknown, malformed or too long, a link's previous code, a revoked link, "
        "a claimed invite, or a deleted inviter. Render the generic landing "
        "then; this endpoint never answers an error for a bad code.\n\n"
        "`Cache-Control: private, no-store`, because the answer changes when the "
        "owner renames or revokes the link."
    ),
)
async def look_up_invite(inviter: InviterDep, response: Response) -> InviteLookupRead:
    response.headers["Cache-Control"] = PRIVATE_NO_STORE
    if inviter is None:
        return InviteLookupRead(inviter=None)
    return InviteLookupRead(
        inviter=InviterRead(first_name=inviter.first_name, avatar_url=inviter.avatar_url)
    )
