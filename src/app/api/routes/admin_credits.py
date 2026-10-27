"""The admin Credits page's reads: find a mentee, their credits, the log, the rules.

Granting stays `POST /admin/credits` in `routes/admin.py`. Same gate and the
same 404 to a caller without a grant (`ADMIN_RESPONSES`).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CreditLogDep, CreditRulesDep, MenteeCreditsDep, MenteeSearchDep
from app.api.routes.admin import ADMIN_RESPONSES
from app.api.schemas.admin_credits import (
    AdminMenteeCreditsRead,
    AdminMenteeRead,
    CreditEntryRead,
    CreditRulesRead,
)
from app.api.schemas.common import Page

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

#: What every entry carries, said once for the two reads that list them.
_ENTRY = (
    "Each entry is one movement of one mentee's balance: `change` is signed, "
    "`balance_after` is the mentee's ledger total after it (it matches their card "
    "except for a credit past its date that the daily expiry has not recorded yet), "
    "and `kind` says what happened: `booking`, `refund` (with `refund_reason`: "
    "`mentor_canceled`, `mentee_canceled`, `canceled` by anybody else, "
    "`mentor_no_show`, or `request_unfulfilled` for a request declined, withdrawn "
    "or expired), `expired`, or a grant by where it came from: `monthly_free`, "
    "`profile_completed` (the starter), `admin_grant` (with `admin` and `note`), "
    "`referral_unlock` (the invite bonus) or `opening_balance` (carried from the "
    "old app). `mentor` is the session's mentor on a booking or a refund, else "
    "null. A person who deleted their account keeps their entries, with both "
    "names null."
)


@router.get(
    "/mentees",
    response_model=Page[AdminMenteeRead],
    summary="Find a mentee",
    description=(
        "Up to 20 mentees whose email, first name, last name or full name contains "
        "`q` (any case), or the one whose id is exactly `q`, ordered by name. "
        "**Mentees only**: a user with a mentor profile is never listed. With no "
        "`q`, the 10 mentees whose credits moved most recently, newest first. "
        "`next_cursor` is always `null`.\n\n"
        "`credits` is the block `GET /me` returns, from the same reader.\n\n"
        "Every live admin grant may look."
    ),
    responses=ADMIN_RESPONSES,
)
async def search_mentees(rows: MenteeSearchDep) -> Page[AdminMenteeRead]:
    return Page(data=rows)


@router.get(
    "/mentees/{user_id}/credits",
    response_model=AdminMenteeCreditsRead,
    summary="A mentee's credits and history",
    description=(
        "The mentee, with the `credits` block `GET /me` returns, and their newest "
        "50 entries, newest first. Older entries are `GET /admin/credit-log` with "
        "`mentee_id` and `history_next_cursor` as `cursor`.\n\n"
        + _ENTRY
        + "\n\n`404` for an id that is nobody, a deleted account or a mentor, and for "
        "a caller without a grant."
    ),
    responses=ADMIN_RESPONSES,
)
async def mentee_credits(panel: MenteeCreditsDep) -> AdminMenteeCreditsRead:
    mentee, (history, cursor) = panel
    return AdminMenteeCreditsRead(mentee=mentee, history=history, history_next_cursor=cursor)


@router.get(
    "/credit-log",
    response_model=Page[CreditEntryRead],
    summary="Every credit change",
    description=(
        "Every movement of every mentee's balance, newest first, 50 a page. `type` "
        "is the log's chips: `admin` (`admin_grant`), `bookings` (`booking`), "
        "`refunds` (`refund`), `monthly` (`monthly_free`, `profile_completed`, "
        "`referral_unlock` and `opening_balance`), or `all`, the default, which is "
        "the only one showing `expired`. `mentee_id` narrows it to one person's "
        "ledger, whoever they are now.\n\n" + _ENTRY
    ),
    responses=ADMIN_RESPONSES,
)
async def read_credit_log(page: CreditLogDep) -> Page[CreditEntryRead]:
    entries, cursor = page
    return Page(data=entries, next_cursor=cursor)


@router.get(
    "/credit-rules",
    response_model=CreditRulesRead,
    summary="The credit rules",
    description=(
        "What the server grants, from its settings: `starter`, the credits every "
        "mentee gets once on finishing onboarding, and `monthly`, what unlocked "
        "mentees get on the 1st of each month. Read-only: they change with the "
        "deployment's settings, not here."
    ),
    responses=ADMIN_RESPONSES,
)
async def read_credit_rules(ladder: CreditRulesDep) -> CreditRulesRead:
    return CreditRulesRead(starter=ladder.starter, monthly=ladder.monthly)
