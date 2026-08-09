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

from fastapi import APIRouter, Response, status

from app.api.deps import (
    AwardsDep,
    CreatedAwardDep,
    CreatedEducationDep,
    CreatedMentorProfileDep,
    DeletedAwardDep,
    DeletedEducationDep,
    DeletedGoalDep,
    EducationDep,
    GoalDep,
    MentorProfileDep,
    ReplacedLanguagesDep,
    UpdatedAwardDep,
    UpdatedEducationDep,
    UpdatedMentorProfileDep,
    UpsertedGoalDep,
    UpsertedProfileDep,
)
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
    "/goal",
    response_model=GoalRead,
    summary="A user's study goal",
    description=(
        "What the mentee is aiming for, with target countries and the kinds of "
        "help they want.\n\n"
        "**Singular: a user has at most one goal.** `mentee_goals.user_id` is "
        "unique, so this returns the goal or 404 rather than a collection that "
        "could only ever hold one item."
    ),
    responses=SCOPED_RESPONSES,
)
async def goal(row: GoalDep) -> GoalRead:
    if row is None:
        raise NotFoundError("this user has no goal")
    return GoalRead.from_row(row)


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


# --------------------------------------------------------------------------
# Writes
#
# **Owner only.** The reads above admit a live admin; these do not. An admin
# reviewing somebody's education is a review, and an admin silently editing it
# is an audit trail nobody has designed — so `OwnerDep` refuses where
# `TargetUserDep` would allow, and opening it later is additive.
# --------------------------------------------------------------------------

WRITE_RESPONSES: dict[int | str, dict[str, str]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "The bearer token is absent, malformed, expired or wrongly signed."
    },
    status.HTTP_404_NOT_FOUND: {
        "description": (
            "No such user or record, **or** not yours. Indistinguishable on "
            "purpose. **A platform admin also receives 404 here** — admins read "
            "these records, and do not write them."
        )
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "The body failed validation."},
}

CONFLICT_RESPONSE: dict[int | str, dict[str, str]] = {
    status.HTTP_409_CONFLICT: {"description": "This user already has a mentor profile."}
}


@router.post(
    "/education",
    status_code=status.HTTP_201_CREATED,
    summary="Add a degree",
    description=(
        "Send `institution_id` when the user picked a school from search. Send "
        "only `school_name_raw` when they typed one we do not hold — the server "
        "matches by name, and **creates an institution awaiting review** if "
        "nothing matches unambiguously, in the same transaction as this entry.\n\n"
        "A name the catalogue carries **twice** is queued, never linked: "
        "`City University` is a real university in three countries, and the "
        "country of study is derived from whichever one is chosen.\n\n"
        "`is_most_recent` clears the flag on the caller's other degrees."
    ),
    responses=WRITE_RESPONSES,
)
async def add_education(created: CreatedEducationDep, response: Response) -> dict[str, object]:
    entry_id, institution_created = created
    response.headers["Location"] = f"/api/v1/users/me/education/{entry_id}"
    # `institution_created` is reported because the client should say so: the
    # school is saved but will not appear in search until an admin has seen it,
    # and silence there reads as search being broken.
    return {"id": str(entry_id), "institution_created": institution_created}


@router.patch(
    "/education/{entry_id}",
    summary="Change a degree",
    description="Only the fields sent are changed. Absent is not null.",
    responses=WRITE_RESPONSES,
)
async def edit_education(changed: UpdatedEducationDep) -> dict[str, bool]:
    if not changed:
        raise NotFoundError("no such education entry")
    return {"updated": True}


@router.delete(
    "/education/{entry_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a degree",
    description=(
        "**Soft** — the row is retained and stops being returned. "
        "`school_name_raw` is what makes an unmatched school recoverable, and a "
        "real delete would take it."
    ),
    responses=WRITE_RESPONSES,
)
async def remove_education(removed: DeletedEducationDep) -> None:
    if not removed:
        raise NotFoundError("no such education entry")


@router.put(
    "/goal",
    summary="Set a user's study goal",
    description=(
        "Creates the goal or replaces it — a user has at most one, so this is a "
        "`PUT` rather than a `POST` that would append.\n\n"
        "`country_ids` and `need_ids` are stored against the user. Omit a list "
        "to leave it alone; send an empty one to clear it."
    ),
    responses=WRITE_RESPONSES,
)
async def set_goal(goal_id: UpsertedGoalDep) -> dict[str, str]:
    return {"id": str(goal_id)}


@router.delete(
    "/goal",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Clear a user's study goal",
    description=(
        "**A real delete** — `mentee_goals` has no soft-delete column, unlike "
        "education and awards. The user's target countries and needs are left "
        "alone: clearing a goal is not a statement about where they want to "
        "study."
    ),
    responses=WRITE_RESPONSES,
)
async def clear_goal(removed: DeletedGoalDep) -> None:
    if not removed:
        raise NotFoundError("this user has no goal")


@router.post(
    "/awards",
    status_code=status.HTTP_201_CREATED,
    summary="Add a scholarship or award",
    description=(
        "Self-reported. `verification_status` is set by the platform, not by the "
        "holder — **nothing verifies an award yet**, so every row is unverified."
    ),
    responses=WRITE_RESPONSES,
)
async def add_award(award_id: CreatedAwardDep, response: Response) -> dict[str, str]:
    response.headers["Location"] = f"/api/v1/users/me/awards/{award_id}"
    return {"id": str(award_id)}


@router.patch("/awards/{award_id}", summary="Change an award", responses=WRITE_RESPONSES)
async def edit_award(changed: UpdatedAwardDep) -> dict[str, bool]:
    if not changed:
        raise NotFoundError("no such award")
    return {"updated": True}


@router.delete(
    "/awards/{award_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove an award",
    description="**Soft** — `user_awards` carries `deleted_at`, unlike goals.",
    responses=WRITE_RESPONSES,
)
async def remove_award(removed: DeletedAwardDep) -> None:
    if not removed:
        raise NotFoundError("no such award")


@router.post(
    "/mentor-profile",
    status_code=status.HTTP_201_CREATED,
    summary="Apply to be a mentor",
    description=(
        "Creates the profile with `approval_status` **pending** — the applicant "
        "does not decide whether they are approved — and flips `primary_role` to "
        "mentor so they land on the right dashboard.\n\n"
        "That flip grants nothing: `primary_role` picks a dashboard and is never "
        "an authorization claim. What a pending mentor may do follows from "
        "`approval_status`.\n\n"
        "**Nothing approves an application yet.** Applications accumulate until "
        "an admin surface exists, alongside institutions awaiting review."
    ),
    responses=WRITE_RESPONSES | CONFLICT_RESPONSE,
)
async def apply_as_mentor(
    profile_id: CreatedMentorProfileDep, response: Response
) -> dict[str, str]:
    response.headers["Location"] = "/api/v1/users/me/mentor-profile"
    return {"id": str(profile_id)}


@router.patch(
    "/mentor-profile",
    summary="Change a mentor profile",
    description=(
        "`approval_status`, `listing_status` and every audit field are not "
        "writable here — a mentor who could approve or relist themselves is not "
        "being reviewed."
    ),
    responses=WRITE_RESPONSES,
)
async def edit_mentor_profile(changed: UpdatedMentorProfileDep) -> dict[str, bool]:
    if not changed:
        raise NotFoundError("this user has no mentor profile")
    return {"updated": True}


@router.patch(
    "/profile",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Change a user's profile",
    description=(
        "Creates the profile row if there is not one — `/me` reports "
        "`has_profile: false` until a user first writes here.\n\n"
        "**`avatar_url` and `banner_url` are not writable.** Images are "
        "content-addressed in Supabase Storage (ADR 0019); accepting a URL would "
        "let a profile point at any host and bypass that entirely. Upload is a "
        "separate build."
    ),
    responses=WRITE_RESPONSES,
)
async def edit_profile(_: UpsertedProfileDep) -> None:
    return None


@router.put(
    "/languages",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Set the languages a user speaks",
    description=(
        "Replaces the whole list — send every language the user speaks, not "
        "just the new one. Omitting a language removes it, which is the only "
        "way a client can express removal.\n\n"
        "**At most one may be primary.** Two would violate a unique index, so "
        "sending two is a 422 naming the problem rather than a 500 to decode. "
        "The same goes for listing one language twice.\n\n"
        "`proficiency` defaults to `fluent` when omitted."
    ),
    responses=WRITE_RESPONSES,
)
async def set_languages(_: ReplacedLanguagesDep) -> None:
    return None
