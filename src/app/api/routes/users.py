"""Endpoints for the authenticated user's own record."""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import (
    BookingWindowDep,
    CurrentUserDep,
    DeletedAccountDep,
    OwnAttributesDep,
    RefundPolicyDep,
    ResponseWindowHoursDep,
)
from app.api.schemas.links import LinkRead
from app.api.schemas.mentors import LanguageRead
from app.api.schemas.onboarding import OnboardingStatusRead
from app.api.schemas.profile import AwardRead, EducationRead, GoalRead, MentorProfileRead
from app.api.schemas.user import BookingCountsRead, CreditsRead, UserProfileRead, UserRead

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
        "`credits` is the dashboard card's block, **present for every caller**: "
        "anyone signed in may book, a mentor included, and every booking spends "
        "a credit. It carries `balance`, `allowance`, `state` and `next_reset_at`; the "
        "client draws the progress bar, so no percentage is published. "
        "`allowance` is `max(steady_state, balance)` — the steady-state "
        "ceiling rather than the monthly grant, so the bar moves when a "
        "credit is spent, and it rises above that when a migrated balance "
        "or a late refund exceeds it. `next_reset_at` is exclusive — the 1st of "
        "the next month at midnight UTC.\n\n"
        "`credits.monthly` and `credits.bonus` split `balance` in two, and always "
        "add up to it. `monthly` is the 1st-of-month grant (and a migrated "
        "opening balance): draw the bar as `monthly.balance` of "
        "`monthly.ceiling`, clamped, since a late refund can briefly exceed it. "
        "`monthly.ceiling` is the grant whether or not the account receives it; "
        "`monthly.unlocked` says whether it does (a mentee goal and an unlock "
        'from a qualifying invite), which tells "not unlocked yet" from '
        '"spent". '
        "`bonus` is every other credit (the starter, the invite bonus, support "
        "grants), grouped by when they expire, soonest first and never-expiring "
        "last. A refunded credit goes back to the part it came from. Spending "
        "order is unchanged: soonest-expiring first.\n\n"
        "`mentee_completed_sessions` counts sessions the caller **received** as "
        "a mentee with status `completed` — never null, and not the mentor "
        "card's `completed_sessions`, which counts sessions given.\n\n"
        "`booking_counts` is `{as_mentor, as_mentee}`, computed live on each "
        "request. `as_mentor` (null without a mentor profile) carries "
        "`awaiting_your_response`, requests waiting on the caller's accept or "
        "decline before their deadline, and `upcoming`, confirmed sessions not yet "
        "started. `as_mentee` (always present, on the same rule as `credits`) "
        "carries `awaiting_mentor`, the caller's own requests still "
        "waiting on a mentor, and `upcoming`. **Badge only the action counts** "
        "(`awaiting_your_response`, `awaiting_mentor`); `upcoming` is information "
        "and would almost always be lit.\n\n"
        "`profile.origin_country` is where you are from, `{code, display_name}` "
        "like every other country in this API, or null when unset. A mentee's "
        "origin is returned here and nowhere else; a mentor's also shows on "
        "their public profile and card, as before.\n\n"
        "`languages` is the languages you speak, `{id, display_name, code}`, "
        "alphabetically, as the public mentor profile lists them; `[]` when "
        "there are none.\n\n"
        "`profile.current_country` is where you live now, the same shape, or "
        "null when unset. It is returned here and nowhere else, for mentees and "
        "mentors alike.\n\n"
        "`session_type_limit` is how many session types a mentor may have (the "
        "default, or support's limit for them), null without a mentor profile.\n\n"
        "The Supabase identifier and the legacy Bubble id are deliberately not "
        "returned: one is a vendor's identifier and the other a migration anchor."
    ),
    responses=ME_RESPONSES,
)
async def read_me(
    user: CurrentUserDep,
    attributes: OwnAttributesDep,
    window: BookingWindowDep,
    refunds: RefundPolicyDep,
    response_hours: ResponseWindowHoursDep,
) -> UserRead:
    links = attributes["links"]
    profile = UserProfileRead.from_row(user) if user["has_profile"] else None
    mentor_profile = attributes["mentor_profile"]
    goal = attributes["goal"]
    return UserRead(
        **user,
        profile=profile,
        education=[EducationRead.from_row(row) for row in attributes["education"]],
        goal=(GoalRead.from_row(goal) if goal is not None else None),
        awards=[AwardRead.from_row(row) for row in attributes["awards"]],
        languages=[LanguageRead.from_row(row) for row in attributes["languages"]],
        links=[LinkRead.of(link) for link in links],
        mentor_profile=(
            MentorProfileRead.from_row(
                mentor_profile, window, owner=True, response_window_hours=response_hours
            )
            if mentor_profile is not None
            else None
        ),
        # Anyone signed in may book (`booked_session` asks for no role), so the
        # mentee half is everyone's: the card and `as_mentee`.
        credits=CreditsRead.model_validate(attributes["credits"]),
        mentee_completed_sessions=attributes["mentee_completed_sessions"],
        onboarding=OnboardingStatusRead.of(*attributes["onboarding"]),
        booking_counts=BookingCountsRead.of(
            attributes["booking_counts"],
            mentor=mentor_profile is not None,
        ),
        mentee_cancel_refund_hours=int(refunds.mentee_cancel_notice.total_seconds() // 3600),
        session_type_limit=(
            mentor_profile["session_type_limit"] if mentor_profile is not None else None
        ),
    )


@router.delete(
    "/me",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete my account",
    description=(
        "Deletes the caller's own account, with **30 days to restore it** "
        'through support. The body is `{"confirm": "DELETE"}`, exactly; '
        "anything else is a `422` and changes nothing.\n\n"
        "In one transaction it ends every open session the caller is part of, "
        "with the reason `account_deleted`: a confirmed session that has not "
        "started is cancelled, a request they sent is withdrawn and one they "
        "received is declined. The other person gets the usual email, and a "
        "mentee gets their credit back when their mentor is the one leaving. "
        "Open means what `booking_counts` counts on `GET /me`: "
        "`upcoming` and the requests still awaiting an answer. A session "
        "already under way is left to settle as attended or missed.\n\n"
        "Unused credits expire, a mentor profile is unlisted, and the account "
        "disappears from every other person's view at once. Then the caller is "
        "signed out of every device. **The token used for this call stops "
        "working at once**: `GET /me` with it answers `404`, as does a second "
        "`DELETE /me`."
    ),
    responses=ME_RESPONSES
    | {
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "`confirm` is missing or is not exactly `DELETE`."
        }
    },
)
async def delete_me(_: DeletedAccountDep) -> None:
    """Everything happens in the dependency; nothing is returned."""
