"""The public invite lookup behind the `/invite/{code}` landing page.

**Unauthenticated, and the first read of user data in the referral domain that
is** (decision 256). The landing renders before sign-in, so it takes no token
and reads none: one sent is ignored, valid or not.

**Any code that cannot be joined through is one `404`**: unknown, malformed, a
link's retired code, a claimed single-use code, or an inviter who was deleted.
Same body, same headers, so a retired handle cannot be told from one that never
existed. The page then renders without a name, never a dead end (owner,
2026-10-15).
"""

from fastapi import APIRouter, Response, status
from fastapi.responses import JSONResponse

from app.api.deps import InviterDep
from app.api.deps.core import PRIVATE_NO_STORE
from app.api.errors import problem
from app.api.schemas.referrals import InviteLookupRead, InviterRead

router = APIRouter(prefix="/api/v1/invites", tags=["referrals"])


@router.get(
    # `:path`, so a code carrying `/` still reaches the one 404 below rather
    # than the router's own.
    "/{code:path}",
    response_model=InviteLookupRead,
    summary="Who an invite is from",
    description=(
        "For the invite landing page, before sign-in. No token needed; one sent "
        "is ignored.\n\n"
        "Resolves a personal link's current code or a single-use invite nobody "
        "has claimed, and returns the inviter's **first name and photo only**, "
        "either of which may be `null`.\n\n"
        "`404` for everything else, with one body: unknown, malformed or too "
        "long, a link's previous code, a claimed invite, or a deleted inviter. "
        "Render the generic landing on any `404`.\n\n"
        "`Cache-Control: private, no-store` on both, because the answer changes "
        "when the owner picks a new handle."
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "No working invite carries that code.",
            "content": {"application/problem+json": {}},
        }
    },
)
async def look_up_invite(
    inviter: InviterDep, response: Response
) -> InviteLookupRead | JSONResponse:
    if inviter is None:
        # Built here, not raised: the 404 must carry the cache header too, and
        # a header set on `response` never reaches the exception handler's.
        missing = problem(
            status_code=status.HTTP_404_NOT_FOUND,
            title="NotFoundError",
            detail="no invite carries that code",
        )
        missing.headers["Cache-Control"] = PRIVATE_NO_STORE
        return missing
    response.headers["Cache-Control"] = PRIVATE_NO_STORE
    return InviteLookupRead(
        inviter=InviterRead(first_name=inviter.first_name, avatar_url=inviter.avatar_url)
    )
