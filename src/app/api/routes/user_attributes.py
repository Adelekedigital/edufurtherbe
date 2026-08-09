"""A user's own education, goals, awards and mentor profile.

**Addressed by user id, not by `/me/…`, and that is what makes them useful.** A
platform admin reviewing an application needs one user's education; a mentee
needs their own. Same resource, same shape, one implementation — the only
difference is who the caller is, and `TargetUserDep` resolves that in a single
statement before any of these handlers runs.

`GET /me` embeds all four for the common case of rendering a profile in one
call. It calls **these same store functions and these same response models**, so
the two paths cannot drift into two shapes — there is a test asserting the
payloads are identical.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import AwardsDep, EducationDep, GoalsDep, MentorProfileDep
from app.api.schemas.common import Page
from app.api.schemas.profile import AwardRead, EducationRead, GoalRead, MentorProfileRead
from app.core.errors import NotFoundError

router = APIRouter(prefix="/api/v1/users/{user_id}", tags=["users"])

# One description, because the answer is the same on all four routes and a
# reader comparing them should not have to spot the difference.
SCOPED_RESPONSES: dict[int | str, dict[str, str]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "The bearer token is absent, malformed, expired or wrongly signed."
    },
    status.HTTP_404_NOT_FOUND: {
        "description": (
            "No such user, **or** not a user you may read. The two are "
            "deliberately indistinguishable: a 403 would confirm the account "
            "exists, which is exactly the fact worth withholding."
        )
    },
}


@router.get(
    "/education",
    response_model=Page[EducationRead],
    summary="A user's education history",
    description=(
        "Degrees held, most recent first.\n\n"
        "`school_name_raw` is **always** present; `institution` may be null when "
        "nothing in the catalogue matched what the user typed. The entry still "
        "displays, and can be linked later without the user re-entering it.\n\n"
        "An institution still awaiting review **is** returned here, unlike in "
        "search — this is the profile of the person who created it, and blanking "
        "their school while an admin looks at it would be the wrong answer."
    ),
    responses=SCOPED_RESPONSES,
)
async def education(rows: EducationDep) -> Page[EducationRead]:
    return Page(data=[EducationRead.from_row(row) for row in rows])


@router.get(
    "/goals",
    response_model=Page[GoalRead],
    summary="A user's study goals",
    description=(
        "What the mentee is aiming for, with target countries and the kinds of "
        "help they want.\n\n"
        "`countries` and `needs` key on the user rather than on an individual "
        "goal — the shape the legacy application used — so a user with two goals "
        "sees the same lists on both."
    ),
    responses=SCOPED_RESPONSES,
)
async def goals(rows: GoalsDep) -> Page[GoalRead]:
    return Page(data=[GoalRead.from_row(row) for row in rows])


@router.get(
    "/awards",
    response_model=Page[AwardRead],
    summary="A user's scholarships and awards",
    description=(
        "Self-reported, newest first. `verification_status` is carried honestly: "
        "**nothing verifies an award yet**, so every migrated row is unverified "
        "and that is a statement about the process, not about the holder."
    ),
    responses=SCOPED_RESPONSES,
)
async def awards(rows: AwardsDep) -> Page[AwardRead]:
    return Page(data=[AwardRead.from_row(row) for row in rows])


@router.get(
    "/mentor-profile",
    response_model=MentorProfileRead,
    summary="A user's mentor profile",
    description=(
        "**404 when the user is not a mentor**, rather than an empty object — "
        "an empty object would claim they are a mentor with nothing filled in, "
        "which is a different and wrong statement.\n\n"
        "`approval_status` and `listing_status` answer the question the owner "
        "actually has: why am I not showing up? They are not authorization "
        "claims — what a mentor may do follows from this row existing."
    ),
    responses=SCOPED_RESPONSES,
)
async def mentor_profile(row: MentorProfileDep) -> MentorProfileRead:
    if row is None:
        raise NotFoundError("this user has no mentor profile")
    return MentorProfileRead.from_row(row)
