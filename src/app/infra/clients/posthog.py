"""PostHog, behind `AnalyticsSink` (PRD 30): plain `httpx`, no SDK.

**Capture** is the public batch endpoint, authenticated by the project token in
the body: ``POST {host}/batch/`` with ``{"api_key", "batch", "historical_migration"}``
(https://posthog.com/docs/api/capture, "Batch events"). ``historical_migration``
is for the one-off backfill, so its events skip the spike detection a burst of
old timestamps would trip. A ``200`` means the payload and token were accepted,
not that every event was ingested; anything else is an `UpstreamError`.

**Erasing a person** is a private endpoint, on the private host
(``https://us.posthog.com`` or ``https://eu.posthog.com``, not the ``i.``
ingestion host: https://posthog.com/docs/api, "the correct domain"):
``POST {api_host}/api/projects/{project_id}/persons/bulk_delete/`` with
``{"distinct_ids": [...], "delete_events": true}`` and a personal API key with
the ``person:write`` scope as a bearer token, answered ``202`` while the person
is removed in the background, and "only events captured before the request
will be deleted" (https://posthog.com/docs/api/persons, "Create persons bulk
delete"). Without the host, project id and key the erase is refused, so the
purge defers the account until they are set; events still send.

Synchronous like every adapter, through the shared client, with the token on
each request. The drain calls it through `asyncio.to_thread`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpx

from app.core.config import Settings, analytics_gaps
from app.core.errors import UpstreamError
from app.domain.ports import AnalyticsSink
from app.infra.clients.posthog_private import PrivateApi, PrivateApiError
from app.infra.http.client import shared
from app.infra.http.upstream import trim_origin, why

__all__ = [
    "NullAnalyticsSink",
    "PersonEraser",
    "PostHogSink",
    "analytics_sink_for",
    "live_analytics_sink",
]

logger = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(15.0)


class NullAnalyticsSink:
    """Analytics is off or unconfigured here: nothing is sent."""

    def capture(self, events: list[dict[str, Any]]) -> None:
        del events

    def delete_person(self, user_id: UUID) -> None:
        del user_id


@dataclass(frozen=True, slots=True)
class PersonEraser:
    """Where and how to erase a person: the private API (`ANALYTICS_API_HOST`,
    `ANALYTICS_PROJECT_ID`, `ANALYTICS_API_KEY`)."""

    api_host: str
    project_id: str
    #: Never in a repr, so never in a log line.
    api_key: str = field(repr=False)


class PostHogSink:
    """Sends events to one PostHog project."""

    def __init__(
        self,
        *,
        host: str,
        project_token: str,
        historical_migration: bool = False,
        client: httpx.Client | None = None,
        eraser: PersonEraser | None = None,
    ) -> None:
        self._host = trim_origin(host)
        self._token = project_token
        self._historical = historical_migration
        self._client = client or shared(TIMEOUT)
        self._eraser = eraser
        self._erase_api = (
            None
            if eraser is None
            else PrivateApi(
                api_host=eraser.api_host,
                project_id=eraser.project_id,
                api_key=eraser.api_key,
                client=self._client,
            )
        )

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

    def delete_person(self, user_id: UUID) -> None:
        if self._erase_api is None:
            # Raised, not skipped: the purge then defers the account, as for a
            # refusal, rather than finish it with the person left behind.
            raise UpstreamError(
                "analytics person not erased: set ANALYTICS_API_HOST, "
                "ANALYTICS_PROJECT_ID and ANALYTICS_API_KEY"
            )
        try:
            self._erase_api.call(
                "POST",
                "/persons/bulk_delete/",
                body={"distinct_ids": [str(user_id)], "delete_events": True},
                parse=False,
            )
        except PrivateApiError as exc:
            raise UpstreamError(f"analytics person erase failed: {exc.reason}") from exc


def analytics_sink_for(settings: Settings, *, client: httpx.Client | None = None) -> AnalyticsSink:
    """PostHog when analytics is on and `analytics_gaps` names nothing, else
    the null sink: the startup check's rule, so the two cannot disagree.
    ``client`` overrides the shared one, for a caller that needs a shorter
    timeout (the error reporter)."""
    host, token = settings.analytics_host, settings.analytics_project_token
    # `analytics_gaps` decides; the last test only narrows the types for mypy.
    if not settings.analytics_enabled or analytics_gaps(settings) or not (host and token):
        return NullAnalyticsSink()
    return PostHogSink(host=host, project_token=token, client=client, eraser=_eraser_for(settings))


def _eraser_for(settings: Settings) -> PersonEraser | None:
    """The erase's settings, or ``None`` while `analytics_gaps(erase=True)` names any."""
    api_host, project_id, key = (
        settings.analytics_api_host,
        settings.analytics_project_id,
        settings.analytics_api_key,
    )
    if analytics_gaps(settings, erase=True) or not (api_host and project_id and key):
        return None
    return PersonEraser(api_host=api_host, project_id=project_id, api_key=key.get_secret_value())


def live_analytics_sink(settings: Settings) -> AnalyticsSink | None:
    """The sink, or ``None`` when analytics is off: queued events then wait,
    pending, as `live_web_push` leaves a push."""
    sink = analytics_sink_for(settings)
    return None if isinstance(sink, NullAnalyticsSink) else sink
