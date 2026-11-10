"""Where the caller came from (PRD 30 PR 2).

**Another router on `/api/v1/me`**, as `me_referrals` records: the prefix is
shared, the subject (analytics, decision 329) is not.
"""

from fastapi import APIRouter, Response, status

from app.api.deps import RecordedAcquisitionDep
from app.api.routes.me_referrals import UNAUTHORISED
from app.api.schemas.attribution import AcquisitionRead

router = APIRouter(prefix="/api/v1/me", tags=["analytics"])


@router.post(
    "/acquisition",
    response_model=AcquisitionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Record where you came from",
    description=(
        "Records how the caller's account arrived, **once**: call it right "
        "after the first sign-in with what the visitor arrived with (the "
        "campaign's tags, the invite code).\n\n"
        "**Write-once.** The first call answers `201` with what was recorded; "
        "every later call answers `200`, changes nothing and returns what is "
        "there. An account moved from the old platform is `migrated` and is "
        "never changed.\n\n"
        "**Never refused for its values**, since they come from a link: an "
        "unknown `source` is recorded as `direct`, an unknown `channel` as "
        "`other`. `migrated` cannot be chosen. A `referral_code` naming "
        "somebody else's live invite records `invite`; the code is not kept."
    ),
    responses=UNAUTHORISED
    | {status.HTTP_200_OK: {"description": "Already recorded; nothing changed."}},
)
async def record_own_acquisition(
    acquisition: RecordedAcquisitionDep, response: Response
) -> AcquisitionRead:
    # Where it is recorded; set on a 200 too, as onboarding's completion does.
    response.headers["Location"] = "/api/v1/me/acquisition"
    if not acquisition.recorded:
        response.status_code = status.HTTP_200_OK
    return AcquisitionRead(source=acquisition.source, channel=acquisition.channel)
