"""The writing assistant (`POST /me/writing-assist`, Onboarding v5 B7, decision 267).

Suggests a bio, drafted from the caller's saved profile or improved from text
they send. **Never saves**: the person keeps a suggestion by saving it through
`PATCH /users/{id}/profile`, with `about_me_drafted: true` and the returned
`prompt_version`, which is how the profile records where the bio came from.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import AssistedTextDep
from app.api.schemas.writing_assist import WritingAssistRead
from app.domain.profile_text import ABOUT_ME_MAX_LENGTH

router = APIRouter(prefix="/api/v1/me", tags=["writing-assist"])


@router.post(
    "/writing-assist",
    response_model=WritingAssistRead,
    summary="Draft or improve your bio",
    description=(
        "`mode: draft` writes a bio from your saved profile: where you are from "
        "and, for `mentee_bio`, your goal's degree, field, start, destinations and "
        "help areas; for `mentor_about`, where and what you studied, your years of "
        "experience and the help you offer. **No name, email or id is sent.** "
        f"`mode: improve` rewrites `text` (required, at most {ABOUT_ME_MAX_LENGTH} "
        "characters) and sends nothing else.\n\n"
        "Returns plain text and the `prompt_version` that wrote it. **Nothing is "
        "saved.** Each press is a fresh suggestion.\n\n"
        "**A daily limit per field**: a deployment setting, 10 by default, counted "
        "over the last 24 hours. Only a call that reaches the assistant counts; a "
        "refused request does not."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "description": "The bearer token is absent, malformed, expired or wrongly signed."
        },
        status.HTTP_404_NOT_FOUND: {
            "description": (
                "`mentor_about` without a mentor profile, or `mentee_bio` with "
                "neither a goal nor a profile."
            )
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": (
                "`errors[0].pointer` is `/text`: missing or blank for `improve`, sent "
                f"for `draft`, longer than {ABOUT_ME_MAX_LENGTH} characters, or too "
                "large for the assistant once encoded."
            )
        },
        status.HTTP_429_TOO_MANY_REQUESTS: {
            "description": (
                "`/problems/rate-limited`: the daily limit for this field is reached. "
                "`Retry-After` says how many seconds until the next is allowed."
            ),
            "headers": {
                "Retry-After": {
                    "description": "Seconds until another request is allowed.",
                    "schema": {"type": "integer", "minimum": 1},
                }
            },
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": (
                "`/problems/writing-assist-unavailable`: the assistant is off on this "
                "deployment, did not answer in time, or could not produce a usable "
                "bio. Try again later, or write your own."
            )
        },
    },
)
async def assist_writing(suggestion: AssistedTextDep) -> WritingAssistRead:
    return suggestion
