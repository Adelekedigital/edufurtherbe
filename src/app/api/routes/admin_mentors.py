"""The admin Mentors page: summary, tabs and one mentor's drawer (decision 297).

Read-only. Every live admin grant may look; approving, declining and listing
stay on `routes/admin.py` with their `mentor_approval` gate. Included after
`admin.router`, and `summary` is declared before `{user_id}`, so neither
`/mentors/pending` nor `/mentors/summary` is read as an id.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import (
    AdminMentorDep,
    AdminMentorRowsDep,
    AdminMentorsSummaryDep,
)
from app.api.routes.admin import ADMIN_RESPONSES
from app.api.schemas.admin_mentors import (
    AdminMentorRead,
    AdminMentorRowRead,
    AdminMentorsSummaryRead,
)
from app.api.schemas.common import Page

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

_TABS = (
    "`applications` (submitted, not yet decided), `approved` (approved and "
    "listed), `paused` (approved and unlisted, by an admin or by the mentor) "
    "and `not_approved` (declined). No mentor is in two; a profile saved "
    "partway through onboarding is in none."
)


@router.get(
    "/mentors/summary",
    response_model=AdminMentorsSummaryRead,
    summary="The Mentors page's cards and tab counts",
    description=(
        f"How many mentors each tab holds ({_TABS}), how long the oldest "
        "application has waited, and the approved mentors' show-up: their mean "
        "rate and how many are `at_risk`. Show-up is the mentor's own "
        "attendance on settled sessions, banded by `show_up_tier` (80% and up "
        "`on_track`, 50-79% `needs_attention`, under 50% `at_risk`).\n\n"
        "Every live admin grant may look."
    ),
    responses=ADMIN_RESPONSES,
)
async def admin_mentors_summary(summary: AdminMentorsSummaryDep) -> AdminMentorsSummaryRead:
    return summary


@router.get(
    "/mentors",
    response_model=Page[AdminMentorRowRead],
    summary="The mentors in one tab",
    description=(
        f"Every mentor in `tab`: {_TABS}\n\n"
        "**The whole tab, not paged** (`next_cursor` is always null): the page "
        "filters and sorts in the browser. Ordered as the tab's default sort: "
        "the longest wait first on `applications`, newest profile first "
        "elsewhere. `q` narrows to mentors whose name or email contains it, "
        "ignoring case.\n\n"
        "Every row carries every field; one that does not apply to the tab is "
        "null. `paused_reason` is an admin's own reason and is never shown to "
        "the mentor; a mentor's own pause has none. Session figures are all "
        "time. Every live admin grant may look."
    ),
    responses=ADMIN_RESPONSES,
)
async def admin_mentor_rows(rows: AdminMentorRowsDep) -> Page[AdminMentorRowRead]:
    return Page(data=rows)


@router.get(
    "/mentors/{user_id}",
    response_model=AdminMentorRead,
    summary="One mentor's review drawer",
    description=(
        "Everything the drawer shows: `status` (the mentor's tab), the facts, "
        "headline and About, education (the `/me` shape), help areas, weekly "
        "hours as declared (the availability shape), all-time `stats`, the five "
        "latest booked sessions with their `outcome`, and why they are paused "
        "(`paused`) or were not approved (`declined`).\n\n"
        "`recent_bookings` counts only sessions that were booked: confirmed, "
        "completed, missed or cancelled, newest start first. A request that was "
        "never agreed is not one. When both people missed a session its outcome "
        "is `missed_by_mentor`.\n\n"
        "`404` for a mentor in no tab (a profile not yet submitted, a deleted "
        "one), for a user who is not a mentor, and for a caller without a grant. "
        "Every live admin grant may look."
    ),
    responses=ADMIN_RESPONSES,
)
async def admin_mentor(mentor: AdminMentorDep) -> AdminMentorRead:
    return mentor
