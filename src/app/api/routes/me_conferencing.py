"""A mentor's default video provider (`/me/conferencing`).

The default holds every offering that chose nothing; a mentor who never chose
gets EduFurther video (`daily`, owner decision 2026-10-01). A Meet link is made
on the **platform's** Google calendar, so choosing Meet needs nothing connected
by the mentor.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import OwnConferencingDep, UpdatedConferencingDep
from app.api.schemas.conferencing import ConferencingRead
from app.domain.meetings import PLATFORM_DEFAULT_PROVIDER

router = APIRouter(prefix="/api/v1/me/conferencing", tags=["availability"])

RESPONSES: dict[int | str, dict[str, str]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "The bearer token is absent, malformed, expired or wrongly signed."
    },
    status.HTTP_404_NOT_FOUND: {"description": "The caller has no mentor profile."},
}


@router.get(
    "",
    response_model=ConferencingRead,
    summary="Your default video provider",
    description=(
        "Where your sessions are held unless an offering says otherwise. "
        "`is_default_choice` is true while you have never chosen and get the "
        f"platform default, `{PLATFORM_DEFAULT_PROVIDER.value}`."
    ),
    responses=RESPONSES,
)
async def read_own_conferencing(saved: OwnConferencingDep) -> ConferencingRead:
    return ConferencingRead.of(saved)


@router.patch(
    "",
    response_model=ConferencingRead,
    summary="Choose your default video provider",
    description=(
        "`daily` (EduFurther video), `google_meet`, or `custom` with your own "
        "`https` room link in `custom_url`. `custom_url` is required for "
        "`custom` and refused for the others (`422`). Replaces your previous "
        "default."
    ),
    responses=RESPONSES,
)
async def update_own_conferencing(saved: UpdatedConferencingDep) -> ConferencingRead:
    return ConferencingRead.of(saved)
