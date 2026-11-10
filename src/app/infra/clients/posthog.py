"""PostHog, behind `AnalyticsSink` (PRD 30): plain `httpx`, no SDK.

**Capture** is the public batch endpoint, authenticated by the project token in
the body: ``POST {host}/batch/`` with ``{"api_key", "batch", "historical_migration"}``
(https://posthog.com/docs/api/capture, "Batch events"). ``historical_migration``
is for the one-off backfill, so its events skip the spike detection a burst of
old timestamps would trip. A ``200`` means the payload and token were accepted,
not that every event was ingested; anything else is an `UpstreamError`.

Synchronous like every adapter, through the shared client, with the token on
each request. The drain calls it through `asyncio.to_thread`.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.config import Settings
from app.core.errors import UpstreamError
from app.domain.ports import AnalyticsSink
from app.infra.http.client import shared
from app.infra.http.upstream import trim_origin, why

__all__ = ["NullAnalyticsSink", "PostHogSink", "analytics_sink_for", "live_analytics_sink"]

logger = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(15.0)


class NullAnalyticsSink:
    """Analytics is off or unconfigured here: nothing is sent."""

    def capture(self, events: list[dict[str, Any]]) -> None:
        del events


class PostHogSink:
    """Sends events to one PostHog project."""

    def __init__(
        self,
        *,
        host: str,
        project_token: str,
        historical_migration: bool = False,
        client: httpx.Client | None = None,
    ) -> None:
        self._host = trim_origin(host)
        self._token = project_token
        self._historical = historical_migration
        self._client = client or shared(TIMEOUT)

    def capture(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        try:
            response = self._client.post(
                f"{self._host}/batch/",
                json={
                    "api_key": self._token,
                    "batch": events,
                    "historical_migration": self._historical,
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("analytics batch refused", extra={"events": len(events)})
            raise UpstreamError(f"analytics capture failed: {why(exc)}") from exc


def analytics_sink_for(settings: Settings) -> AnalyticsSink:
    """PostHog when analytics is on and configured, else the null sink. The
    one place either is chosen."""
    if not (
        settings.analytics_enabled and settings.analytics_host and settings.analytics_project_token
    ):
        return NullAnalyticsSink()
    return PostHogSink(host=settings.analytics_host, project_token=settings.analytics_project_token)


def live_analytics_sink(settings: Settings) -> AnalyticsSink | None:
    """The sink, or ``None`` when analytics is off: queued events then wait,
    pending, as `live_web_push` leaves a push."""
    sink = analytics_sink_for(settings)
    return None if isinstance(sink, NullAnalyticsSink) else sink
