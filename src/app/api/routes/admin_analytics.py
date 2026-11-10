"""The admin Analytics page's reads (PRD 31, decision 324).

One route per tab, so a tab loads only its own numbers. Each carries the
`window` read and every alert firing now. Behind `ADMIN_ANALYTICS_ENABLED`:
off, every route is `404`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from app.api.deps.admin_analytics import (
    AnalyticsCreditsDep,
    AnalyticsOverviewDep,
    AnalyticsSessionsDep,
    require_analytics_on,
)
from app.api.deps.core import PRIVATE_NO_STORE
from app.api.routes.admin import ADMIN_RESPONSES
from app.api.schemas.admin_analytics import (
    AnalyticsCreditsRead,
    AnalyticsOverviewRead,
    AnalyticsSessionsRead,
)

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
    "/overview",
    response_model=AnalyticsOverviewRead,
    summary="Analytics: overview",
    description=(
        "The platform North Star (completed sessions, by `starts_at`, with a series), "
        "its sub-tracks, and the seven feature North Stars, each a percentage with "
        "an 8-point sparkline:\n\n"
        "- `booking`: requests made in the window that became completed sessions "
        "(a moved request by where its moves ended)\n"
        "- `explore`: null until it reads PostHog\n"
        "- `messaging`: chats started in the window answered by the other person "
        "within 24 hours\n"
        "- `mentor_supply`: listed mentors whose card shows a free time within 14 "
        "days, now (no `previous`, no sparkline)\n"
        "- `invites`: invites made in the window whose invitee finished their profile\n"
        "- `mentee_activation`: mentees who signed up in the window (migrated accounts "
        "are not sign-ups) with a completed session within 14 days\n"
        "- `mentor_activation`: mentors approved in the window who are taking "
        "bookings now (an application must be bookable to be submitted, so this "
        "catches a mentor who stopped)\n\n" + _SECTION
    ),
    responses=_RESPONSES,
)
async def analytics_overview(
    read: AnalyticsOverviewDep, response: Response
) -> AnalyticsOverviewRead:
    response.headers["Cache-Control"] = PRIVATE_NO_STORE
    return read


@router.get(
    "/sessions",
    response_model=AnalyticsSessionsRead,
    summary="Analytics: sessions",
    description=(
        "KPIs: `requests` made in the window (a move is not a request); "
        "`acceptance_rate`, accepted over accepted, declined and expired, for "
        "requests decided in the window; `median_reply_hours` from request to "
        "accept or decline, for replies in the window; `show_up_rate`, completed "
        "over completed and no-show, for sessions starting in the window; "
        "`repeat_booking_rate`, mentees with two or more completed sessions in the "
        "60 days ending at the window's end over those with one; `average_rating`, "
        "the mean session value of reviews written in the window (one decimal); "
        "`review_rate`, those reviews over sessions completed in the window (can "
        "pass 100); `reports` made in the window. Guardrails: `no_show_rate`, "
        "`request_expiry_rate` (expired over decided, in the window) and "
        "`credit_expiry_rate` (expired over granted, refunds left out).\n\n" + _SECTION
    ),
    responses=_RESPONSES,
)
async def analytics_sessions(
    read: AnalyticsSessionsDep, response: Response
) -> AnalyticsSessionsRead:
    response.headers["Cache-Control"] = PRIVATE_NO_STORE
    return read
