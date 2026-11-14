"""Where the caller came from, and their answer to analytics cookies (PRD 30 PR 2).

**Another router on `/api/v1/me`**, as `me_referrals` records: the prefix is
shared, the subject (analytics, decision 329) is not.
"""

from fastapi import APIRouter, Response, status

from app.api.deps import RecordedAcquisitionDep, RecordedConsentDep
from app.api.routes.me_referrals import UNAUTHORISED
from app.api.schemas.attribution import AcquisitionRead, AnalyticsConsentRead

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


@router.put(
    "/consents/analytics",
    response_model=AnalyticsConsentRead,
    summary="Answer analytics cookies",
    description=(
        "Records the caller's answer to analytics cookies, replacing any "
        "earlier one: a decline is kept as an answer, and a later grant "
        "replaces it. `GET /me` → `analytics_consent` reads it back.\n\n"
        "`method` says how it was given (see the field), and a later answer "
        "replaces it too.\n\n"
        "It governs the app's own tracking in the browser. The outcomes the "
        "server records (a booking, a session held) are the platform's own "
        "records, by account id and never by name, and are counted in product "
        "analytics whatever the answer."
    ),
    responses=UNAUTHORISED,
)
async def answer_analytics_consent(consent: RecordedConsentDep) -> AnalyticsConsentRead:
    return AnalyticsConsentRead.from_row(consent)
