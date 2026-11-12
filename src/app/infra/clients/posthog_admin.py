"""PostHog's private API for the dashboard sync (decision 341), with the
personal key `ANALYTICS_API_KEY` (insight and dashboard read and write).
Each call is one documented operation:

- insights: https://posthog.com/docs/api/insights (`name`, `query`, `dashboards`,
  `description`, `tags`, `deleted`; an insight leaves a dashboard when it leaves
  `dashboards`; "Hard delete of this model is not allowed. Use a patch API call
  to set "deleted" to true")
- dashboards: https://posthog.com/docs/api/dashboards (`name`, `description`,
  `tags`, `deleted`)
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx

from app.core.config import Settings
from app.core.errors import ConfigurationError, UpstreamError
from app.infra.clients.posthog_private import PrivateApi, PrivateApiError
from app.infra.clients.posthog_reader import private_api
from app.infra.http.client import shared

__all__ = ["PostHogAdmin", "PostHogAnswerError", "posthog_admin_for"]

TIMEOUT = httpx.Timeout(30.0, connect=5.0)
#: More pages than any list here has: past it, the paging is broken.
MAX_PAGES = 100


class PostHogAnswerError(UpstreamError):
    """PostHog answered, but not in the shape its docs give."""


def _row(value: Any, what: str) -> dict[str, Any]:
    """An item as documented: an object with an integer `id`."""
    if not isinstance(value, dict) or isinstance(value.get("id"), bool):
        raise PostHogAnswerError(f"PostHog sent {what} that is not an item")
    if not isinstance(value.get("id"), int):
        raise PostHogAnswerError(f"PostHog sent {what} without an id")
    return value


class PostHogAdmin:
    """One project's insights and dashboards. ``written`` counts the writes
    that landed, per kind, so a failed run can say what it already changed."""

    def __init__(
        self, *, api_host: str, project_id: str, api_key: str, client: httpx.Client | None = None
    ) -> None:
        self._api = PrivateApi(
            api_host=api_host,
            project_id=project_id,
            api_key=api_key,
            client=client or shared(TIMEOUT),
        )
        self.written: Counter[str] = Counter()

    def _call(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        try:
            answer = self._api.call(method, path, params=params, body=body)
        except PrivateApiError as exc:
            raise UpstreamError(f"PostHog {method} {path} failed: {exc.reason}") from exc
        if method != "GET":
            self.written[path.strip("/").split("/")[0]] += 1
        return answer

    def _write(self, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return _row(self._call(method, path, body=body), f"a {method} answer")

    def _list(self, path: str) -> Iterator[dict[str, Any]]:
        """Every row, following each page's `next`. Only its query (offset,
        limit, cursor) is used, always against this project's own path, so
        the key goes nowhere an answer names. A repeated query or more than
        `MAX_PAGES` pages stops the list."""
        params: dict[str, str] = {}
        asked: set[tuple[tuple[str, str], ...]] = set()
        for _ in range(MAX_PAGES):
            asked.add(tuple(sorted(params.items())))
            page = self._call("GET", path, params or None)
            if not isinstance(page, dict) or not isinstance(page.get("results"), list):
                raise PostHogAnswerError(f"PostHog sent a {path} page without results")
            for row in page["results"]:
                yield _row(row, f"a {path} row")
            following = page.get("next")
            if not following:
                return
            if not isinstance(following, str):
                raise PostHogAnswerError(f"PostHog sent a {path} page with a broken next")
            params = dict(parse_qsl(urlsplit(following).query))
            if tuple(sorted(params.items())) in asked:
                raise PostHogAnswerError(f"PostHog repeated a {path} page")
        raise PostHogAnswerError(f"PostHog listed {path} past {MAX_PAGES} pages")

    def insights(self) -> list[dict[str, Any]]:
        return list(self._list("/insights/"))

    def create_insight(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._write("POST", "/insights/", body)

    def update_insight(self, insight_id: int, body: dict[str, Any]) -> dict[str, Any]:
        path = f"/insights/{int(insight_id)}/"
        return self._write("PATCH", path, body)

    def dashboards(self) -> list[dict[str, Any]]:
        return list(self._list("/dashboards/"))

    def create_dashboard(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._write("POST", "/dashboards/", body)

    def update_dashboard(self, dashboard_id: int, body: dict[str, Any]) -> dict[str, Any]:
        path = f"/dashboards/{int(dashboard_id)}/"
        return self._write("PATCH", path, body)


def posthog_admin_for(settings: Settings, client: httpx.Client | None = None) -> PostHogAdmin:
    """The private API client, or `ConfigurationError` naming what is unset."""
    api = private_api(settings)
    if api is None:
        raise ConfigurationError(
            "set ANALYTICS_API_HOST, ANALYTICS_PROJECT_ID (a number) and ANALYTICS_API_KEY"
        )
    host, project, key = api
    return PostHogAdmin(api_host=host, project_id=project, api_key=key, client=client)
