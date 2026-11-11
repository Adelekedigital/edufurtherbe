"""The admin Analytics page's reads (PRD 31, decision 324).

One route per tab, so a tab loads only its own numbers. Each carries the
`window` read and every alert firing now. Behind `ADMIN_ANALYTICS_ENABLED`:
off, every route is `404`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from app.api.deps.admin_analytics import (
    AnalyticsBehaviourDep,
    AnalyticsCreditsDep,
    AnalyticsFunnelDep,
    AnalyticsGrowthDep,
    AnalyticsOverviewDep,
    AnalyticsSessionsDep,
    AnalyticsSupplyDep,
    require_analytics_on,
)
from app.api.deps.core import PRIVATE_NO_STORE
from app.api.errors import retry_after_header
from app.api.routes.admin import ADMIN_RESPONSES
from app.api.schemas.admin_analytics import (
    AnalyticsBehaviourRead,
    AnalyticsCreditsRead,
    AnalyticsFunnelRead,
    AnalyticsGrowthRead,
    AnalyticsOverviewRead,
    AnalyticsSessionsRead,
    AnalyticsSupplyRead,
)
from app.domain.analytics_behaviour import FUNNELS

router = APIRouter(
    prefix="/api/v1/admin/analytics",
    tags=["admin"],
    dependencies=[Depends(require_analytics_on)],
)

#: Said once for every section: the window, compare, exclusions and alerts.
_SECTION = (
    "**Window**: `window=7d|30d|90d` (ending today, default `30d`), or `from` and "
    "`to` as dates, both inclusive (one day sends the same date twice; `from` "
    "no earlier than 2024-06-01, `to` no later than today). The previous period "
    "has the same number of days and ends the day before. `compare=false` makes "
    "every `previous` null.\n\n"
    "**`tz`**: an IANA time zone such as `Africa/Lagos`, default `UTC`. Today, "
    "the dates, each day's midnight (a daylight-saving day is 23 or 25 hours) and "
    "the series and sparkline points are read in it, and `window.tz` echoes it. "
    "The alerts' yesterday and Growth's `months` stay UTC.\n\n"
    "A bad value, `tz` included, is a `422` of type `/problems/analytics-window`, "
    "with `errors` naming the parameter.\n\n"
    "**Left out of every number**: admins and demo accounts. Preview sessions "
    "write nothing, so nothing of theirs is counted.\n\n"
    "**`alerts`**: every alert firing now, newest first, whatever the window: no "
    "booking requests for 24 hours, no sign-ups for 24 hours (migrated accounts "
    "are not sign-ups), and more than 20 emails, notifications or events given up on yesterday "
    "(UTC), and more than 10% of yesterday's (UTC) log-in codes failed, read from "
    "PostHog and left out when PostHog does not answer.\n\n"
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
        "- `explore` (PostHog): searchers who then viewed a profile or started a booking; "
        "`contribution`, those who requested one within 7 days. Null when PostHog is down\n"
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
        "requests decided in the window (a move going back for approval is not a "
        "second decision); `median_reply_hours` from request to "
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


@router.get(
    "/growth",
    response_model=AnalyticsGrowthRead,
    summary="Analytics: growth",
    description=(
        "- `signups`: accounts made in the window, `mentees` and `mentors` by their "
        "role; migrated accounts are not sign-ups\n"
        "- `by_source`, `by_channel`: always empty until sign-ups record where they "
        "came from\n"
        "- `invites`: invites made in the window: all of them (`invited`), those "
        "whose invitee signed up (`joined`), finished their profile (`qualified`), "
        "has any session as a mentee (`first_booking`) or a completed one "
        "(`first_completed`). `cost_per_completed` is always null: no credit is "
        "tied to an invite. `top_inviter` is the inviter, not deleted, with the "
        "most `joined`, ties by name; null when nobody joined\n"
        "- `months`: sign-ups in each of the last 6 UTC calendar months, oldest "
        "first, this one included, whatever the window\n"
        "- `migrated`: accounts carried over from the old app, all time, with `on` "
        "always null (no cutover date is recorded); null with none\n"
        "- `completed_total`: completed sessions starting in the window\n"
        "- `by_topic`: those sessions under each service offering their session "
        "type covers, every offering in the platform's order; a session counts "
        "under each of its type's topics, and one with no type under none\n"
        "- `by_stage`: the same by application stage, each of the six in order and "
        "then `not_set`, for a session whose type names no stage or that has no "
        "type\n\n" + _SECTION
    ),
    responses=_RESPONSES,
)
async def analytics_growth(read: AnalyticsGrowthDep, response: Response) -> AnalyticsGrowthRead:
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


#: Each funnel, its window and its breakdowns, from the definitions.
_FUNNELS = (
    "".join(
        f"- `{f.name}` ({f.window_days} days): " + ", ".join(f"`{key}`" for key in f.by) + "\n"
        for f in FUNNELS.values()
    )
    + "\n"
)

#: The PostHog routes' `503`.
_POSTHOG = (
    "**From PostHog**, kept 10 minutes. Not set up or not answering: `503` "
    "`/problems/analytics-upstream` with `Retry-After`.\n\n"
)
_POSTHOG_RESPONSES = _RESPONSES | {
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "description": "PostHog is not set up or not answering.",
        "headers": retry_after_header("Seconds to wait before asking again."),
    }
}


@router.get(
    "/funnels/{name}",
    response_model=AnalyticsFunnelRead,
    summary="Analytics: a behaviour funnel",
    description=(
        "People entering the funnel in the window who reached each step within "
        "`window_days` (untracked: null). `by` must be one the funnel offers, else `422`:\n\n"
        + _FUNNELS
        + "Each step's `layer` is the row it is drawn in: `primary`, the path, or "
        "`secondary`, the outcomes beside it. `figures` sit beside the funnel (see "
        "the schema), `[]` for a funnel with none.\n\n"
        "**`mentor_activation` is read from this database**, not PostHog, and is "
        "never a `503`: mentors approved in the window, at their first approval in "
        "it; of them, those with weekly hours still there at or after approval; of "
        "those, those with a published session type of their own (not the one "
        "onboarding made). The outcomes count from approval: a booking request to "
        "them within 30 days, then of those a completed session as mentor within 30 "
        "days. `by` is their sign-up source. Only its `profile_link_shared` figure "
        "reads PostHog, and is null when PostHog does not answer.\n\n"
        "Every other funnel: " + _POSTHOG + _SECTION
    ),
    responses=_POSTHOG_RESPONSES,
)
async def analytics_funnel(read: AnalyticsFunnelDep, response: Response) -> AnalyticsFunnelRead:
    response.headers["Cache-Control"] = PRIVATE_NO_STORE
    return read


@router.get(
    "/behaviour",
    response_model=AnalyticsBehaviourRead,
    summary="Analytics: behaviour event cards",
    description=(
        "The seven event cards for the window (no `previous`), and `replays_url`, this "
        "environment's session replays.\n\n" + _POSTHOG + _SECTION
    ),
    responses=_POSTHOG_RESPONSES,
)
async def analytics_behaviour(
    read: AnalyticsBehaviourDep, response: Response
) -> AnalyticsBehaviourRead:
    response.headers["Cache-Control"] = PRIVATE_NO_STORE
    return read
