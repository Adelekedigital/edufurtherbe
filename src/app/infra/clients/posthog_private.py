"""PostHog's private API, said once: one project's base URL, the personal key
as a Bearer header on each request, and a failure that never carries the key
(`why(..., secret=key)`). The reader, the person erase and the dashboard sync
all send through it (decision 341)."""

from __future__ import annotations

from typing import Any

import httpx

from app.infra.http.upstream import trim_origin, why

__all__ = ["PrivateApi", "PrivateApiError"]


class PrivateApiError(Exception):
    """A request failed: ``reason`` has the key cut out, ``status`` is the
    answer's status when there was one, ``failure`` the error's kind."""

    def __init__(self, reason: str, status: int | None, failure: str) -> None:
        super().__init__(reason)
        self.reason, self.status, self.failure = reason, status, failure


class PrivateApi:
    def __init__(
        self, *, api_host: str, project_id: str, api_key: str, client: httpx.Client
    ) -> None:
        self.origin = trim_origin(api_host)
        self.base = f"{self.origin}/api/projects/{project_id}"
        self._key = api_key
        self._client = client

    def call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        timeout: httpx.Timeout | None = None,
        parse: bool = True,
    ) -> Any:
        """``path`` under the project, never a URL an answer gave, so the key
        goes nowhere else. With ``parse`` the answer's JSON, a body that is
        not JSON failing like a refusal; without, ``None`` on any 2xx."""
        url = f"{self.base}{path}"
        response: httpx.Response | None = None
        try:
            response = self._client.request(
                method,
                url,
                params=params,
                json=body,
                headers={"Authorization": f"Bearer {self._key}"},
                timeout=timeout if timeout is not None else self._client.timeout,
            )
            response.raise_for_status()
            return response.json() if parse else None
        except (httpx.HTTPError, ValueError) as exc:
            raise PrivateApiError(
                why(exc, response, secret=self._key),
                None if response is None else response.status_code,
                type(exc).__name__,
            ) from exc
