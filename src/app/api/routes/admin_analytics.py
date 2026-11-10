"""The admin Analytics page's reads (PRD 31, decision 324).

One route per tab, so a tab loads only its own numbers. Each carries the
`window` read and every alert firing now. Behind `ADMIN_ANALYTICS_ENABLED`:
off, every route is `404`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from app.api.deps.admin_analytics import (
    AnalyticsCreditsDep,
    AnalyticsSupplyDep,
    require_analytics_on,
)
from app.api.deps.core import PRIVATE_NO_STORE
from app.api.routes.admin import ADMIN_RESPONSES
from app.api.schemas.admin_analytics import AnalyticsCreditsRead, AnalyticsSupplyRead

router = APIRouter(
    prefix="/api/v1/admin/analytics",
    tags=["admin"],
    dependencies=[Depends(require_analytics_on)],
)

#: Said once for every section: the window, compare, exclusions and alerts.
_SECTION = (
    "**Window**: `window=7d|30d|90d` (ending today, default `30d`), or `from` and "
    "`to` as UTC dates, both inclusive (one day sends the same date twice; `from` "
    "no earlier than 2024-06-01, `to` no later than today). The previous period "
    "has the same number of days and ends the day before. `compare=false` makes "
    "every `previous` null. A bad value is a `422` of type "
    "`/problems/analytics-window`, with `errors` naming the parameter.\n\n"
    "**Left out of every number**: admins and demo accounts. Preview sessions "
    "write nothing, so nothing of theirs is counted.\n\n"
    "**`alerts`**: every alert firing now, newest first, whatever the window: no "
    "booking requests for 24 hours, no sign-ups for 24 hours (migrated accounts "
    "are not sign-ups), and more than 20 emails, notifications or events given up on yesterday "
    "(UTC). The log-in code alert arrives with the PostHog reads.\n\n"
    "Every live admin grant may read; anybody else gets `404`, as does everybody "
    "while the page is switched off. `Cache-Control: private, no-store`."
)

_RESPONSES = ADMIN_RESPONSES | {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "The window cannot be read."}
}


@router.get(
    "/credits",
    response_model=AnalyticsCreditsRead,
    summary="Analytics: credits",
    description=(
        "What happened to credits in the window, in total and by source, read off "
        "the ledger by each movement's own time. No change against the previous "
        "period: the tab shows none.\n\n" + _SECTION
    ),
    responses=_RESPONSES,
)
async def analytics_credits(read: AnalyticsCreditsDep, response: Response) -> AnalyticsCreditsRead:
    response.headers["Cache-Control"] = PRIVATE_NO_STORE
    return read


@router.get(
    "/supply",
    response_model=AnalyticsSupplyRead,
    summary="Analytics: mentor supply",
    description=(
        "Whether there are enough mentors, and where mentees go unserved.\n\n"
        "**`cohort`**: mentor applications submitted in the window, profile and "
        "account not deleted; `applied` is all of them, and `approved`, `listed` "
        "and `bookable` how many are approved, approved and listed, and taking "
        "bookings **now**. **`median_review_days`**: for approvals and declines "
        "made in the window, the median days from submission to decision, one "
        "decimal half up; an application never submitted is skipped; null with "
        "none. **`unlisted`**: unlistings in the window, except those that come "
        "with a decline or a deleted account.\n\n"
        "**`by_country`** and **`by_school`**, now whatever the window: live "
        "mentees aiming for each goal country, or catalogue school (approved, not "
        "merged), against mentors taking bookings who guide that country, or have "
        "an education entry at that school. `key` is the country's ISO code or the "
        "school's id. Only places with a mentee, at most five: no bookable mentor "
        "first, by name; then most mentees per mentor; then by name.\n\n"
        "No change against the previous period: the tab shows none, so `compare` "
        "changes nothing.\n\n" + _SECTION
    ),
    responses=_RESPONSES,
)
async def analytics_supply(read: AnalyticsSupplyDep, response: Response) -> AnalyticsSupplyRead:
    response.headers["Cache-Control"] = PRIVATE_NO_STORE
    return read
