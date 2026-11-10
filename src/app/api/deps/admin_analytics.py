"""The admin Analytics page (PRD 31, decision 324): the switch, the window a
request asks for, the alerts, and one dependency per section.

Every live admin grant may read (`QueueViewerDep`, as the Mentors and Credits
pages). With `ADMIN_ANALYTICS_ENABLED` off, every route is `404` before the
token is read, as previews are.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Path, Query, Request

from app.api.deps.core import BookingWindowDep, QueueViewerDep, SessionDep, _configured
from app.api.schemas.admin_analytics import (
    AnalyticsBehaviourRead,
    AnalyticsCreditsRead,
    AnalyticsFunnelRead,
    AnalyticsGrowthRead,
    AnalyticsOverviewRead,
    AnalyticsSessionsRead,
    AnalyticsSupplyRead,
)
from app.core.errors import AnalyticsUpstreamError, NotFoundError
from app.domain.analytics import (
    Alert,
    Feature,
    Period,
    TabCache,
    newest_first,
    resolve_compare,
    resolve_period,
    yesterday,
)
from app.domain.analytics_behaviour import (
    FUNNEL_NAMES,
    BehaviourFigures,
    LoginCodes,
    behaviour_tab,
    breakdown_for,
    explore_feature,
    explore_windows,
    funnel_named,
    funnel_tab,
    login_alert,
)
from app.domain.ports import AnalyticsReader
from app.infra.clients.posthog_reader import analytics_reader_for, replay_url_for
from app.infra.db.analytics_alerts import active_alerts
from app.infra.db.analytics_credits import credits_tab
from app.infra.db.analytics_growth import growth_tab
from app.infra.db.analytics_scope import internal_ids
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


def analytics_reader(request: Request) -> AnalyticsReader:
    """The reader wired on `app.state`, else the one settings choose."""
    wired: AnalyticsReader | None = getattr(request.app.state, "analytics_reader", None)
    return wired if wired is not None else analytics_reader_for(_configured(request))


AnalyticsReaderDep = Annotated[AnalyticsReader, Depends(analytics_reader)]


#: What an optional read keeps in place of figures when PostHog failed.
_FAILED = object()
#: How long an optional read leaves a failed PostHog alone: as long as the
#: `503` routes tell the page to wait.
FAILED_FOR = dt.timedelta(seconds=AnalyticsUpstreamError.RETRY_AFTER_SECONDS)


async def _kept[T](
    request: Request,
    key: tuple[object, ...],
    now: dt.datetime,
    compute: Callable[[], Awaitable[T]],
    *,
    optional: bool = False,
) -> T:
    """Figures from this app's `TabCache`, or computed and kept. An ``optional``
    read (one a database tab adds) also keeps a failure for `FAILED_FOR`."""
    cache: TabCache | None = getattr(request.app.state, "analytics_cache", None)
    if cache is None:
        cache = request.app.state.analytics_cache = TabCache()
    hit = cache.get(key, now)
    if hit is _FAILED:
        raise AnalyticsUpstreamError("product analytics failed a moment ago")
    if hit is not None:
        return hit  # type: ignore[return-value]
    try:
        value = await compute()
    except AnalyticsUpstreamError:
        if optional:
            cache.put(key, _FAILED, now, keep_for=FAILED_FOR)
        raise
    cache.put(key, value, now)
    return value


async def _cached[T](
    request: Request, tab: str, ask: AnalyticsAsk, compute: Callable[[], Awaitable[T]]
) -> T:
    """A tab's figures, kept per tab, dates and compare."""
    key = (tab, ask.period.first, ask.period.last, ask.compare)
    return await _kept(request, key, ask.now, compute)


async def _login_alert(
    request: Request, reader: AnalyticsReader, session: SessionDep, now: dt.datetime
) -> Alert | None:
    """The log-in alert from yesterday's kept codes, or none when PostHog cannot
    answer (PRD 31: skipped, not failed)."""
    today = now.date()
    day = yesterday(today)

    async def compute() -> LoginCodes:
        internal = await internal_ids(session)
        return await asyncio.to_thread(reader.login_codes, day.window, internal)

    try:
        codes = await _kept(request, ("login_codes", day.first), now, compute, optional=True)
    except AnalyticsUpstreamError:
        return None
    return login_alert(codes, today)


async def analytics_alerts(
    request: Request, ask: AnalyticsAskDep, session: SessionDep, reader: AnalyticsReaderDep
) -> list[Alert]:
    """Every alert firing now: the database's, and the log-in alert when the
    product-analytics source answers."""
    alerts = await active_alerts(session, ask.now)
    return newest_first([*alerts, await _login_alert(request, reader, session, ask.now)])


AlertsDep = Annotated[list[Alert], Depends(analytics_alerts)]


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
    reader: AnalyticsReaderDep,
) -> AnalyticsOverviewRead:
    # The Explore read runs beside the database's, in its own thread and
    # never on this session: its ids are read first.
    explore_read = asyncio.create_task(_explore(request, ask, reader, await internal_ids(session)))
    try:
        tab = await _cached(
            request,
            "overview",
            ask,
            lambda: overview_tab(session, ask.period, ask.compare, booking_window),
        )
    except BaseException:
        explore_read.cancel()
        raise
    explore = await explore_read
    if explore is not None:
        features = [explore if f.key == "explore" else f for f in tab.features]
        tab = replace(tab, features=features)
    return AnalyticsOverviewRead.of(ask.period, alerts, tab)


async def _explore(
    request: Request, ask: AnalyticsAsk, reader: AnalyticsReader, internal: list[UUID]
) -> Feature | None:
    """The Explore card, or ``None`` when PostHog cannot answer (the card stays
    empty, nothing else changes)."""
    windows = explore_windows(ask.period)
    try:
        figures = await _kept(
            request,
            ("explore", ask.period.first, ask.period.last),
            ask.now,
            lambda: asyncio.to_thread(reader.explore, windows, internal),
            optional=True,
        )
    except AnalyticsUpstreamError:
        return None
    return explore_feature(figures, ask.compare)


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


async def analytics_funnel(
    name: Annotated[
        str,
        Path(description=", ".join(f"`{n}`" for n in FUNNEL_NAMES) + "; another is `404`."),
    ],
    request: Request,
    ask: AnalyticsAskDep,
    alerts: AlertsDep,
    reader: AnalyticsReaderDep,
    session: SessionDep,
    by: Annotated[
        str | None,
        Query(description="One of the funnel's breakdowns; `all` (the default) is everyone."),
    ] = None,
) -> AnalyticsFunnelRead:
    funnel = funnel_named(name)
    chosen = breakdown_for(funnel, by)

    async def compute() -> tuple[list[int], list[int] | None]:
        internal = await internal_ids(session)
        current = await asyncio.to_thread(
            reader.funnel, funnel, chosen, ask.period.window, internal
        )
        if not ask.compare:
            return current, None
        before = ask.period.previous().window
        return current, await asyncio.to_thread(reader.funnel, funnel, chosen, before, internal)

    current, previous = await _cached(request, f"funnel:{funnel.name}:{chosen}", ask, compute)
    return AnalyticsFunnelRead.of(ask.period, alerts, funnel_tab(funnel, chosen, current, previous))


AnalyticsFunnelDep = Annotated[AnalyticsFunnelRead, Depends(analytics_funnel)]


async def analytics_behaviour(
    request: Request,
    ask: AnalyticsAskDep,
    alerts: AlertsDep,
    reader: AnalyticsReaderDep,
    session: SessionDep,
) -> AnalyticsBehaviourRead:
    async def compute() -> BehaviourFigures:
        internal = await internal_ids(session)
        return await asyncio.to_thread(reader.behaviour, ask.period.window, internal)

    figures = await _cached(request, "behaviour", ask, compute)
    replay = replay_url_for(_configured(request))
    return AnalyticsBehaviourRead.of(ask.period, alerts, behaviour_tab(figures), replay)


AnalyticsBehaviourDep = Annotated[AnalyticsBehaviourRead, Depends(analytics_behaviour)]
