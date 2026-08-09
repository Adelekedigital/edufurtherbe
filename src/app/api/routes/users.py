"""Endpoints for the authenticated user's own record."""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import CurrentUserDep, OwnAttributesDep
from app.api.schemas.profile import AwardRead, EducationRead, GoalRead, MentorProfileRead
from app.api.schemas.user import UserProfileRead, UserRead

router = APIRouter(prefix="/api/v1", tags=["users"])

# Documented per route rather than left to the function name, because these
# descriptions are the contract the Next.js client is built against — a reader
# of /docs should not have to open this file to learn how a call can fail.
ME_RESPONSES: dict[int | str, dict[str, str]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "The bearer token is absent, malformed, expired or wrongly signed."
    },
    status.HTTP_404_NOT_FOUND: {
        "description": (
            "The token is valid but no account is linked to it. Every migrated user is "
            "in this state until auth provisioning runs."
        )
    },
}


@router.get(
    "/me",
    response_model=UserRead,
    summary="The signed-in user",
    description=(
        "Returns the caller's own record, with their profile when one exists, "
        "and their education, goals, awards and mentor profile embedded — so a "
        "profile page renders in one call rather than five.\n\n"
        "Each embedded collection is the **same shape** the matching "
        "`/users/{user_id}/...` endpoint returns, built by the same query and "
        "the same response model. `mentor_profile` is null for the great "
        "majority of users, who are not mentors.\n\n"
        "`primary_role` decides which dashboard to land on and is **not** an "
        "authorization claim — permissions come from whether the relevant profile "
        "row exists, never from this field. `is_admin` reflects a live, unrevoked "
        "grant in `admin_users`.\n\n"
        "The Supabase identifier and the legacy Bubble id are deliberately not "
        "returned: one is a vendor's identifier and the other a migration anchor."
    ),
    responses=ME_RESPONSES,
)
async def read_me(user: CurrentUserDep, attributes: OwnAttributesDep) -> UserRead:
    profile = UserProfileRead(**user) if user["has_profile"] else None
    mentor_profile = attributes["mentor_profile"]
    return UserRead(
        **user,
        profile=profile,
        education=[EducationRead.from_row(row) for row in attributes["education"]],
        goals=[GoalRead.from_row(row) for row in attributes["goals"]],
        awards=[AwardRead.from_row(row) for row in attributes["awards"]],
        mentor_profile=(
            MentorProfileRead.from_row(mentor_profile) if mentor_profile is not None else None
        ),
    )
