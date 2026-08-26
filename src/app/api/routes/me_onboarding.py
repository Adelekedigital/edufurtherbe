"""``POST /api/v1/me/onboarding/completion`` — finishing onboarding.

**Its own module, and a fourth router on ``/api/v1/me``.** `users.py` is the
signed-in user's record; this is a transition in their lifecycle that pays a
credit. Keeping it separate is the same call `me_session_types.py` records: the
prefix is shared, the subject is not.

**A sub-resource rather than a flag.** ``POST .../completion`` says a thing
happened at a moment; ``PATCH /me/onboarding {"completed": true}`` would invite
``false`` and there is no such transition — nobody un-finishes onboarding, and a
route that appears to allow it is a route somebody eventually calls.
"""

from fastapi import APIRouter, Response, status

from app.api.deps import CompletedOnboardingDep, OwnOnboardingDep
from app.api.schemas.onboarding import OnboardingRead

router = APIRouter(prefix="/api/v1/me", tags=["onboarding"])

COMPLETION_RESPONSES: dict[int | str, dict[str, str]] = {
    status.HTTP_200_OK: {
        "description": (
            "Onboarding was already finished. The original `completed_at` is "
            "returned unchanged and no second credit is granted."
        )
    },
    status.HTTP_401_UNAUTHORIZED: {
        "description": "The bearer token is absent, malformed, expired or wrongly signed."
    },
    status.HTTP_409_CONFLICT: {
        "description": (
            "The profile does not yet meet the bar. Carries the problem type "
            "`/problems/onboarding-incomplete`, which means send the user back "
            "to the profile form — retrying the same request cannot succeed."
        )
    },
}


@router.post(
    "/onboarding/completion",
    response_model=OnboardingRead,
    status_code=status.HTTP_201_CREATED,
    summary="Finish onboarding",
    description=(
        "Marks the caller's onboarding finished and grants their starter "
        "credit — one, and it never expires.\n\n"
        "**Idempotent.** The first call answers `201` and creates the credit; "
        "every call after answers `200`, returns the original `completed_at` "
        "unchanged, and grants nothing. That guarantee is a database "
        "constraint rather than a check, so two concurrent calls cannot both "
        "succeed.\n\n"
        "Refused with `409` and the problem type "
        "`/problems/onboarding-incomplete` unless the caller has a profile and "
        "a role-appropriate profile beside it — a mentee goal or a mentor "
        "profile. The starter is granted for finishing a profile rather than "
        "for signing up, because signing up is free and finishing one is work.\n\n"
        "The credit appears on `GET /api/v1/me` under `credits`."
    ),
    responses=COMPLETION_RESPONSES,
)
async def complete_onboarding(
    completion: CompletedOnboardingDep, response: Response
) -> OnboardingRead:
    """201 the first time, 200 thereafter.

    The distinction is drawn from whether a lot was *created*, not from whether
    a row already existed — a user whose completion the ETL wrote but who never
    received a credit gets their credit and a 201, which is the honest answer.
    """
    # Points at the collection rather than at this sub-resource, which is the
    # idiom `me_session_types` settled: a client that has to guess where the
    # thing it just made now lives is being told less than the response could
    # tell it. Set on 200 as well — it names where the state lives either way.
    response.headers["Location"] = "/api/v1/me/onboarding"
    if not completion.granted:
        response.status_code = status.HTTP_200_OK
    return OnboardingRead.model_validate(completion)


@router.get(
    "/onboarding",
    response_model=OnboardingRead,
    summary="The caller's onboarding record",
    description=(
        "Where the caller got to, and when they finished if they have.\n\n"
        "`404` when they have never started — deliberately, rather than an "
        "empty record: never having begun is a different fact from having "
        "begun and got nowhere, and `last_step` is null in both."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "description": "The bearer token is absent, malformed, expired or wrongly signed."
        },
        status.HTTP_404_NOT_FOUND: {"description": "Onboarding has not been started."},
    },
)
async def read_own_onboarding(onboarding: OwnOnboardingDep) -> OnboardingRead:
    return OnboardingRead.model_validate(onboarding)
