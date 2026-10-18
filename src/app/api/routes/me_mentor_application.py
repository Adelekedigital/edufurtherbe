"""``/api/v1/me/mentor-application`` — submitting a mentor application.

**Its own module**, like `me_onboarding`: saving a mentor profile is a record
on `/users/me/mentor-profile`; submitting it is a transition that tells the
admins, and the two must not be one call (Mentor Onboarding PRD, B2).
"""

from fastapi import APIRouter, status

from app.api.deps import SeenApprovalDep, SubmittedApplicationDep
from app.api.schemas.onboarding import ApprovalSeenRead, MentorApplicationRead
from app.domain.mentor_application import MentorApplicationItem

router = APIRouter(prefix="/api/v1/me/mentor-application", tags=["mentor-application"])

_ITEMS = ", ".join(f"`{item.value}`" for item in MentorApplicationItem)

SUBMIT_RESPONSES: dict[int | str, dict[str, str]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "The bearer token is absent, malformed, expired or wrongly signed."
    },
    status.HTTP_404_NOT_FOUND: {"description": "The caller has no mentor profile."},
    status.HTTP_409_CONFLICT: {
        "description": (
            "Either `/problems/mentor-application-incomplete`: the application "
            "is missing something, named by the `missing` member in step order "
            f"({_ITEMS}); send the mentor to the first one's step. Or "
            "`/problems/mentor-application-decided`: it was already approved or "
            "declined, so show the outcome."
        )
    },
}


@router.post(
    "/submit",
    response_model=MentorApplicationRead,
    summary="Submit my mentor application",
    description=(
        "Submits the caller's application and tells the admins who can decide "
        "it, **once**.\n\n"
        "**Idempotent while pending**: a second call answers `200` with the "
        "original `submitted_at` and sends nothing, so a retried submit never "
        "alerts twice. Edits after submitting save as usual and need no second "
        "submit.\n\n"
        f"A complete application holds every item `missing` can name ({_ITEMS}); "
        "About is optional. Weekly hours and a switched-on session type are "
        "among them because approval lists a mentor at once, so they must be "
        "bookable then."
    ),
    responses=SUBMIT_RESPONSES,
)
async def submit_mentor_application(submitted: SubmittedApplicationDep) -> MentorApplicationRead:
    return MentorApplicationRead(
        submitted_at=submitted.submitted_at, approval_status=submitted.approval_status
    )


@router.post(
    "/approved-seen",
    response_model=ApprovalSeenRead,
    summary="Record that I saw my approval",
    description=(
        "Called when the approved screen renders, so it shows **once per "
        "account**, on any device. Idempotent: the first time is kept and every "
        "later call returns it. `409` unless the application is approved; `404` "
        "with no mentor profile. `GET /api/v1/me` → `mentor_profile` → "
        "`approved_seen_at` reads it back."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: SUBMIT_RESPONSES[status.HTTP_401_UNAUTHORIZED],
        status.HTTP_404_NOT_FOUND: SUBMIT_RESPONSES[status.HTTP_404_NOT_FOUND],
        status.HTTP_409_CONFLICT: {"description": "The application is not approved."},
    },
)
async def approved_seen(seen: SeenApprovalDep) -> ApprovalSeenRead:
    return ApprovalSeenRead(approved_seen_at=seen)
