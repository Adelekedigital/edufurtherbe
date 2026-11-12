"""PostHog's private API for the dashboard sync (decision 341), with the
personal key `ANALYTICS_API_KEY` (cohort, insight and dashboard read and write,
person read). Each call is one documented operation:

- insights: https://posthog.com/docs/api/insights (`name`, `query`, `dashboards`,
  `description`, `tags`; an insight leaves a dashboard when it leaves `dashboards`)
- dashboards: https://posthog.com/docs/api/dashboards (`name`, `description`, `tags`)
- cohorts: https://posthog.com/docs/api/cohorts; members by person UUID, "not
  distinct IDs" (https://posthog.com/docs/open-api-spec/cohorts_add_persons_to_static_cohort_partial_update.md),
  so a distinct id is looked up first (https://posthog.com/docs/api/persons, `distinct_id`).

The key travels per request and is cut out of any error (`why`).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx

from app.core.config import Settings
from app.core.errors import ConfigurationError, UpstreamError
from app.infra.clients.posthog_reader import private_api
from app.infra.http.client import shared
from app.infra.http.upstream import trim_origin, why

__all__ = ["PAGE", "PostHogAdmin", "posthog_admin_for"]

TIMEOUT = httpx.Timeout(30.0, connect=5.0)
#: Rows per list request.
PAGE = 100
#: The most person ids one add takes.
ADD_LIMIT = 1000


class PostHogAdmin:
    """One project's cohorts, insights and dashboards."""

    def __init__(
        self, *, api_host: str, project_id: str, api_key: str, client: httpx.Client | None = None
    ) -> None:
        self._base = f"{trim_origin(api_host)}/api/projects/{project_id}"
        self._key = api_key
        self._client = client or shared(TIMEOUT)

    def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        response: httpx.Response | None = None
        try:
            response = self._client.request(
                method,
                f"{self._base}{path}",
                params=params,
                json=body,
                headers={"Authorization": f"Bearer {self._key}"},
            )
            response.raise_for_status()
            return response.json() if response.content else None
        except (httpx.HTTPError, ValueError) as exc:
            reason = why(exc, response, secret=self._key)
            raise UpstreamError(f"PostHog {method} {path} failed: {reason}") from exc

    def _list(self, path: str, **params: Any) -> Iterator[dict[str, Any]]:
        """Every row, a page at a time by offset (never the answer's `next` URL)."""
        offset = 0
        while True:
            page = self._call("GET", path, params={**params, "limit": PAGE, "offset": offset})
            rows = page.get("results") or []
            yield from rows
            if len(rows) < PAGE:
                return
            offset += PAGE

    def insights(self) -> list[dict[str, Any]]:
        return list(self._list("/insights/"))

    def create_insight(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._call("POST", "/insights/", body=body)  # type: ignore[no-any-return]

    def update_insight(self, insight_id: int, body: dict[str, Any]) -> dict[str, Any]:
        return self._call("PATCH", f"/insights/{int(insight_id)}/", body=body)  # type: ignore[no-any-return]

    def dashboards(self) -> list[dict[str, Any]]:
        return list(self._list("/dashboards/"))

    def create_dashboard(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._call("POST", "/dashboards/", body=body)  # type: ignore[no-any-return]

    def update_dashboard(self, dashboard_id: int, body: dict[str, Any]) -> dict[str, Any]:
        return self._call("PATCH", f"/dashboards/{int(dashboard_id)}/", body=body)  # type: ignore[no-any-return]

    def cohorts(self) -> list[dict[str, Any]]:
        return list(self._list("/cohorts/", type="static"))

    def create_cohort(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._call("POST", "/cohorts/", body=body)  # type: ignore[no-any-return]

    def members(self, cohort_id: int) -> list[dict[str, Any]]:
        """The cohort's persons, each with its `uuid` and `distinct_ids`."""
        return list(self._list(f"/cohorts/{int(cohort_id)}/persons/", format="json"))

    def person_uuid(self, distinct_id: str) -> str | None:
        """The person PostHog knows ``distinct_id`` as, or ``None`` before any event."""
        rows = self._call("GET", "/persons/", params={"distinct_id": distinct_id})
        found = rows.get("results") or []
        return str(found[0]["uuid"]) if found else None

    def add_members(self, cohort_id: int, person_uuids: list[str]) -> None:
        for at in range(0, len(person_uuids), ADD_LIMIT):
            self._call(
                "PATCH",
                f"/cohorts/{int(cohort_id)}/add_persons_to_static_cohort/",
                body={"person_ids": person_uuids[at : at + ADD_LIMIT]},
            )

    def remove_member(self, cohort_id: int, person_uuid: str) -> None:
        self._call(
            "PATCH",
            f"/cohorts/{int(cohort_id)}/remove_person_from_static_cohort/",
            body={"person_id": person_uuid},
        )


def posthog_admin_for(settings: Settings, client: httpx.Client | None = None) -> PostHogAdmin:
    """The private API client, or `ConfigurationError` naming what is unset."""
    api = private_api(settings)
    if api is None:
        raise ConfigurationError(
            "set ANALYTICS_API_HOST, ANALYTICS_PROJECT_ID (a number) and ANALYTICS_API_KEY"
        )
    host, project, key = api
    return PostHogAdmin(api_host=host, project_id=project, api_key=key, client=client)
