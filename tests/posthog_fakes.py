"""A fake PostHog Query API for the Behaviour tests (decision 331): answers and
a reader over an `httpx.MockTransport`, so nothing reaches a network."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import httpx

from app.infra.clients.posthog_reader import CARDS_QUERY, PostHogReader

Handler = Callable[[httpx.Request], httpx.Response]
HOST = "https://posthog.example.test"
KEY = "phx_personal_key"
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


def posthog_reader(handler: Handler, project: str = "42") -> tuple[PostHogReader, list[Any]]:
    """A reader over ``handler``, and the requests it was sent."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(record))
    return PostHogReader(api_host=HOST + "/", project_id=project, api_key=KEY, client=client), seen
