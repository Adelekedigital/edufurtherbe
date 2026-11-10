"""The admin Analytics page (PRD 31, decision 324): the switch, the window a
request asks for, the alerts, and one dependency per section.

Every live admin grant may read (`QueueViewerDep`, as the Mentors and Credits
pages). With `ADMIN_ANALYTICS_ENABLED` off, every route is `404` before the
token is read, as previews are.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Query, Request

from app.api.deps.core import BookingWindowDep, QueueViewerDep, SessionDep, _configured
from app.api.schemas.admin_analytics import (
    AnalyticsCreditsRead,
    AnalyticsGrowthRead,
    AnalyticsOverviewRead,
    AnalyticsSessionsRead,
    AnalyticsSupplyRead,
)
from app.core.errors import NotFoundError
from app.domain.analytics import Alert, Period, TabCache, resolve_compare, resolve_period
from app.infra.db.analytics_alerts import active_alerts
from app.infra.db.analytics_credits import credits_tab
from app.infra.db.analytics_growth import growth_tab
from app.infra.db.analytics_supply import supply_tab
from app.infra.db.analytics_tabs import overview_tab, sessions_tab


def require_analytics_on(request: Request) -> None:
    """Every analytics route is `404` while the page is switched off."""
    if not _configured(request).admin_analytics_enabled:
        raise NotFoundError("no such endpoint")


@dataclass(frozen=True, slots=True)
class AnalyticsAsk:
    """What a section request asks for, resolved at one instant."""

    now: dt.datetime
    period: Period
    compare: bool


def analytics_ask(
    _: QueueViewerDep,
    window: Annotated[
        str | None,
        Query(
            description="`7d`, `30d` or `90d`, ending today. Default `30d`. Not with `from`/`to`."
        ),
    ] = None,
    from_: Annotated[
        str | None,
        Query(
            alias="from",
            description=(
                "First UTC day of a custom range, `YYYY-MM-DD`, no earlier than 2024-06-01. "
                "Needs `to`; one day sends the same date for both."
            ),
        ),
    ] = None,
    to: Annotated[
        str | None,
        Query(description="Last UTC day of a custom range, inclusive, no later than today."),
    ] = None,
    compare: Annotated[
        str | None,
        Query(description="`true` (default) or `false`: whether `previous` figures are sent."),
    ] = None,
) -> AnalyticsAsk:
    now = dt.datetime.now(dt.UTC)
    period = resolve_period(now.date(), preset=window, first=from_, last=to)
    return AnalyticsAsk(now=now, period=period, compare=resolve_compare(compare))


AnalyticsAskDep = Annotated[AnalyticsAsk, Depends(analytics_ask)]


async def analytics_alerts(ask: AnalyticsAskDep, session: SessionDep) -> list[Alert]:
    return await active_alerts(session, ask.now)


AlertsDep = Annotated[list[Alert], Depends(analytics_alerts)]


async def _cached[T](
    request: Request, tab: str, ask: AnalyticsAsk, compute: Callable[[], Awaitable[T]]
) -> T:
    """A tab's figures from this app's `TabCache`, or computed and kept."""
    cache: TabCache | None = getattr(request.app.state, "analytics_cache", None)
    if cache is None:
        cache = request.app.state.analytics_cache = TabCache()
    key = (tab, ask.period.first, ask.period.last, ask.compare)
    hit = cache.get(key, ask.now)
    if hit is not None:
        return hit  # type: ignore[return-value]
    value = await compute()
    cache.put(key, value, ask.now)
    return value


async def analytics_credits(
    request: Request, ask: AnalyticsAskDep, alerts: AlertsDep, session: SessionDep
) -> AnalyticsCreditsRead:
    tab = await _cached(request, "credits", ask, lambda: credits_tab(session, ask.period.window))
    return AnalyticsCreditsRead.of(ask.period, alerts, tab)


AnalyticsCreditsDep = Annotated[AnalyticsCreditsRead, Depends(analytics_credits)]


async def analytics_overview(
    request: Request,
    ask: AnalyticsAskDep,
    alerts: AlertsDep,
    session: SessionDep,
    booking_window: BookingWindowDep,
) -> AnalyticsOverviewRead:
    tab = await _cached(
        request,
        "overview",
        ask,
        lambda: overview_tab(session, ask.period, ask.compare, booking_window),
    )
    return AnalyticsOverviewRead.of(ask.period, alerts, tab)


AnalyticsOverviewDep = Annotated[AnalyticsOverviewRead, Depends(analytics_overview)]


async def analytics_sessions(
    request: Request, ask: AnalyticsAskDep, alerts: AlertsDep, session: SessionDep
) -> AnalyticsSessionsRead:
    tab = await _cached(
        request, "sessions", ask, lambda: sessions_tab(session, ask.period, ask.compare)
    )
    return AnalyticsSessionsRead.of(ask.period, alerts, tab)


AnalyticsSessionsDep = Annotated[AnalyticsSessionsRead, Depends(analytics_sessions)]


async def analytics_growth(
    request: Request, ask: AnalyticsAskDep, alerts: AlertsDep, session: SessionDep
) -> AnalyticsGrowthRead:
    tab = await _cached(
        request, "growth", ask, lambda: growth_tab(session, ask.period, ask.compare, ask.now)
    )
    return AnalyticsGrowthRead.of(ask.period, alerts, tab)


AnalyticsGrowthDep = Annotated[AnalyticsGrowthRead, Depends(analytics_growth)]


async def analytics_supply(
    request: Request, ask: AnalyticsAskDep, alerts: AlertsDep, session: SessionDep
) -> AnalyticsSupplyRead:
    tab = await _cached(request, "supply", ask, lambda: supply_tab(session, ask.period.window))
    return AnalyticsSupplyRead.of(ask.period, alerts, tab)


AnalyticsSupplyDep = Annotated[AnalyticsSupplyRead, Depends(analytics_supply)]
