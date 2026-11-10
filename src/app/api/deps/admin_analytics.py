"""The admin Analytics page (PRD 31, decision 324): the switch, the window a
request asks for, the alerts, and one dependency per section.

Every live admin grant may read (`QueueViewerDep`, as the Mentors and Credits
pages). With `ADMIN_ANALYTICS_ENABLED` off, every route is `404` before the
token is read, as previews are.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Query, Request

from app.api.deps.core import QueueViewerDep, SessionDep, _configured
from app.api.schemas.admin_analytics import AnalyticsCreditsRead, AnalyticsSupplyRead
from app.core.errors import NotFoundError
from app.domain.analytics import Alert, Period, resolve_compare, resolve_period
from app.infra.db.analytics_alerts import active_alerts
from app.infra.db.analytics_credits import credits_tab
from app.infra.db.analytics_supply import supply_tab


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


async def analytics_credits(
    ask: AnalyticsAskDep, alerts: AlertsDep, session: SessionDep
) -> AnalyticsCreditsRead:
    tab = await credits_tab(session, ask.period.window)
    return AnalyticsCreditsRead.of(ask.period, alerts, tab)


AnalyticsCreditsDep = Annotated[AnalyticsCreditsRead, Depends(analytics_credits)]


async def analytics_supply(
    ask: AnalyticsAskDep, alerts: AlertsDep, session: SessionDep
) -> AnalyticsSupplyRead:
    tab = await supply_tab(session, ask.period.window)
    return AnalyticsSupplyRead.of(ask.period, alerts, tab)


AnalyticsSupplyDep = Annotated[AnalyticsSupplyRead, Depends(analytics_supply)]
