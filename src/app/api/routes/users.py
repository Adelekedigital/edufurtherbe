"""Endpoints for the authenticated user's own record."""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import BookingWindowDep, CurrentUserDep, OwnAttributesDep
from app.api.schemas.common import AvatarFocusRead
from app.api.schemas.profile import AwardRead, EducationRead, GoalRead, MentorProfileRead
from app.api.schemas.user import CreditsRead, UserProfileRead, UserRead

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
            "The token is valid but has no live account and nothing to create one "
            "from: it carries no email, or its account was deleted. A first "
            "sign-in with an email creates the account (settled decision #178)."
        )
    },
    status.HTTP_409_CONFLICT: {
        "description": (
            "`/problems/account-exists`: a first sign-in whose email belongs to "
            "an account this sign-in is not linked to. Never linked or merged "
            "automatically; the person should contact support."
        )
    },
}


@router.get(
    "/me",
    response_model=UserRead,
    summary="The signed-in user",
    description=(
        "Returns the caller's own record, with their profile when one exists, "
        "and their education, goal, awards and mentor profile embedded — so a "
        "profile page renders in one call rather than five.\n\n"
        "Each embedded collection is the **same shape** the matching "
        "`/users/{user_id}/...` endpoint returns, built by the same query and "
        "the same response model. `mentor_profile` is null for the great "
        "majority of users, who are not mentors.\n\n"
        "`primary_role` decides which dashboard to land on and is **not** an "
        "authorization claim — permissions come from whether the relevant profile "
        "row exists, never from this field. `is_admin` reflects a live, unrevoked "
        "grant in `admin_users`.\n\n"
        "`credits` is the dashboard card's block and is **null unless the caller "
        "has a mentee goal** — the same predicate the monthly grant uses, and "
        'deliberately not "is not a mentor", since a dual-role user is both. '
        "It carries `balance`, `allowance`, `state` and `next_reset_at`; the "
        "client draws the progress bar, so no percentage is published. "
        "`allowance` is `max(steady_state, balance)` — the steady-state "
        "ceiling rather than the monthly grant, so the bar moves when a "
        "credit is spent, and it rises above that when a migrated balance "
        "or a late refund exceeds it. `next_reset_at` is exclusive — the 1st of "
        "the next month at midnight UTC.\n\n"
        "`mentee_completed_sessions` counts sessions the caller **received** as "
        "a mentee with status `completed` — never null, and not the mentor "
        "card's `completed_sessions`, which counts sessions given.\n\n"
        "`mentor_pending_bookings` counts requests still waiting on the caller's "
        "answer as a mentor — `pending_mentor_approval` with the answer deadline "
        "not yet passed — for the sidebar's Bookings badge. `null` when the caller "
        "has no mentor profile. Computed live on each request.\n\n"
        "The Supabase identifier and the legacy Bubble id are deliberately not "
        "returned: one is a vendor's identifier and the other a migration anchor."
    ),
    responses=ME_RESPONSES,
)
async def read_me(
    user: CurrentUserDep, attributes: OwnAttributesDep, window: BookingWindowDep
) -> UserRead:
    profile = (
        UserProfileRead(
            **user,
            avatar_focus=AvatarFocusRead.of(user["avatar_focus_x"], user["avatar_focus_y"]),
        )
        if user["has_profile"]
        else None
    )
    mentor_profile = attributes["mentor_profile"]
    # Bound before the call rather than walrused inside it: `credits` reads it
    # too, and a keyword argument that depends on an earlier argument's
    # side effect breaks silently the first time somebody reorders them.
    goal = attributes["goal"]
    return UserRead(
        **user,
        profile=profile,
        education=[EducationRead.from_row(row) for row in attributes["education"]],
        goal=(GoalRead.from_row(goal) if goal is not None else None),
        awards=[AwardRead.from_row(row) for row in attributes["awards"]],
        mentor_profile=(
            MentorProfileRead.from_row(mentor_profile, window)
            if mentor_profile is not None
            else None
        ),
        # The card belongs to a mentee. The predicate is *having a mentee goal*,
        # not *not being a mentor* — authorization here is profile existence, so
        # a dual-role user is both, and a negative predicate would hide the card
        # from somebody who can book.
        credits=(CreditsRead.model_validate(attributes["credits"]) if goal is not None else None),
        mentee_completed_sessions=attributes["mentee_completed_sessions"],
        mentor_pending_bookings=attributes["mentor_pending_bookings"],
    )
