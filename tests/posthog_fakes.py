"""A fake PostHog over an `httpx.MockTransport`, so nothing reaches a network:
the Query API for the Behaviour tests (decision 331), the insights and
dashboards the dashboard sync writes (decision 341), and the one recording
client every PostHog test sends through."""

from __future__ import annotations

import itertools
import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from app.infra.clients.posthog_admin import PostHogAdmin
from app.infra.clients.posthog_reader import CARDS_QUERY, PostHogReader

Handler = Callable[[httpx.Request], httpx.Response]
HOST = "https://posthog.example.test"
KEY = "phx_personal_key"
#: The project path every private-API request is under.
PREFIX = "/api/projects/42"
#: The cards query's columns, in its order, read off the query itself.
CARD_COLUMNS = re.findall(r"AS (\w+)", CARDS_QUERY)


def body_of(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)  # type: ignore[no-any-return]


def rows(columns: list[str], *values: list[Any]) -> httpx.Response:
    return httpx.Response(200, json={"columns": columns, "results": [list(v) for v in values]})


def funnel_answer(*counts: int) -> httpx.Response:
    """Steps out of order on purpose: `order` decides, not position."""
    results = [{"order": i, "count": c} for i, c in enumerate(counts)]
    return httpx.Response(200, json={"results": list(reversed(results))})


def recording_client(
    seen: list[httpx.Request] | None = None, *, status: int = 200, handler: Handler | None = None
) -> httpx.Client:
    """A client that appends each request to ``seen`` and answers it with
    ``handler``, or with an empty JSON body and ``status``."""

    def record(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return handler(request) if handler else httpx.Response(status, json={})

    return httpx.Client(transport=httpx.MockTransport(record))


def posthog_reader(handler: Handler, project: str = "42") -> tuple[PostHogReader, list[Any]]:
    """A reader over ``handler``, and the requests it was sent."""
    seen: list[httpx.Request] = []
    client = recording_client(seen, handler=handler)
    return PostHogReader(api_host=HOST + "/", project_id=project, api_key=KEY, client=client), seen


class FakePostHog:
    """In-memory insights and dashboards behind a MockTransport, listed a
    few rows a page with a `next` link, as PostHog pages."""

    def __init__(self, page: int = 4) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {"insights": [], "dashboards": []}
        self.writes: list[tuple[str, str, Any]] = []
        self.ids = itertools.count(1)
        self.page = page
        self.keep_tags = True
        self.keep_keys = True
        #: PostHog's `next` names its own host, which need not be ours.
        self.next_host = "https://app.posthog.example.test"
        #: A whole other list URL for `next`, when set.
        self.next_url: str | None = None

    def add(self, kind: str, **row: Any) -> dict[str, Any]:
        row = {"id": next(self.ids), "deleted": False, **row}
        self.rows[kind].append(row)
        return row

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        url = urlparse(str(request.url))
        parts = url.path.removeprefix(PREFIX).strip("/").split("/")
        body = json.loads(request.content) if request.content else None
        kind = parts[0]
        if request.method == "GET":
            offset = int(parse_qs(url.query).get("offset", ["0"])[0])
            rows = self.rows[kind][offset : offset + self.page]
            more = offset + self.page < len(self.rows[kind])
            at = self.next_url or f"{self.next_host}{url.path}"
            following = f"{at}?limit={self.page}&offset={offset + self.page}"
            return httpx.Response(200, json={"results": rows, "next": following if more else None})
        self.writes.append((request.method, url.path, body))
        if len(parts) == 1:
            created = self.add(kind, **body)
            if not self.keep_tags:
                created["tags"] = []
            if not self.keep_keys:
                created["description"] = "managed-key lost"
            return httpx.Response(201, json=created)
        row = next(r for r in self.rows[kind] if r["id"] == int(parts[1]))
        row.update(body)
        return httpx.Response(200, json=row)

    def admin(self) -> PostHogAdmin:
        client = httpx.Client(transport=httpx.MockTransport(self))
        return PostHogAdmin(api_host=HOST + "/", project_id="42", api_key=KEY, client=client)
