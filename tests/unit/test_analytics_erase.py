"""PRD 30 PR 2: erasing a purged account's person at PostHog.

The vendor is an `httpx.MockTransport`; nothing here reaches a network.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from uuid import UUID

import httpx
import pytest

from app.core.config import Settings, analytics_gaps, env_key
from app.core.errors import UpstreamError
from app.infra.clients.posthog import (
    NullAnalyticsSink,
    PersonEraser,
    PostHogSink,
    analytics_sink_for,
)

USER = UUID("0190aaaa-0000-7000-8000-000000000001")
HOST = "https://us.i.posthog.com"
API_HOST = "https://us.posthog.com"
TOKEN = "phc_project_token"  # noqa: S105 - a public ingestion token, made up
KEY = "phx_personal_key"  # made up
ERASER = PersonEraser(api_host=API_HOST + "/", project_id="4242", api_key=KEY)
ON = {"analytics_enabled": True, "analytics_host": HOST, "analytics_project_token": TOKEN}
ERASE = {"analytics_api_host": API_HOST, "analytics_project_id": "4242", "analytics_api_key": KEY}


@pytest.fixture(autouse=True)
def no_process_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in Settings.model_fields:
        monkeypatch.delenv(env_key(name), raising=False)
    yield


def settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def recording(seen: list[httpx.Request], status: int = 202) -> httpx.Client:
    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json={"persons_queued_for_deletion": 1})

    return httpx.Client(transport=httpx.MockTransport(answer))


def test_delete_person_posts_a_bulk_delete_by_distinct_id_with_its_events() -> None:
    seen: list[httpx.Request] = []
    sink = PostHogSink(host=HOST, project_token=TOKEN, client=recording(seen), eraser=ERASER)

    sink.delete_person(USER)

    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == f"{API_HOST}/api/projects/4242/persons/bulk_delete/"
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert json.loads(request.content) == {"distinct_ids": [str(USER)], "delete_events": True}


@pytest.mark.parametrize("status", [400, 401, 403, 500])
def test_a_refused_erase_is_an_upstream_error(status: int) -> None:
    sink = PostHogSink(
        host=HOST, project_token=TOKEN, client=recording([], status=status), eraser=ERASER
    )

    with pytest.raises(UpstreamError, match=str(status)):
        sink.delete_person(USER)


def test_without_the_erase_settings_nothing_is_sent_and_the_erase_is_refused() -> None:
    """Refused, so the purge defers the account rather than leave the person."""
    seen: list[httpx.Request] = []
    sink = PostHogSink(host=HOST, project_token=TOKEN, client=recording(seen))

    with pytest.raises(UpstreamError, match="ANALYTICS_PROJECT_ID"):
        sink.delete_person(USER)

    assert seen == []


def test_the_null_sink_erases_nothing() -> None:
    NullAnalyticsSink().delete_person(USER)


def test_the_key_never_appears_in_the_erasers_repr() -> None:
    assert KEY not in repr(ERASER)


def test_erase_gaps_are_named_only_when_asked_and_only_while_on() -> None:
    assert analytics_gaps(settings(**ON)) == []
    assert analytics_gaps(settings(**ON), erase=True) == [
        "ANALYTICS_API_HOST is not set",
        "ANALYTICS_PROJECT_ID is not set",
        "ANALYTICS_API_KEY is not set",
    ]
    assert analytics_gaps(settings(**ON, **ERASE), erase=True) == []
    assert analytics_gaps(settings(), erase=True) == []


def test_the_factory_arms_the_erase_only_when_configured() -> None:
    armed = analytics_sink_for(settings(**ON, **ERASE))
    unarmed = analytics_sink_for(settings(**ON))

    assert isinstance(armed, PostHogSink) and armed._eraser is not None
    assert armed._eraser.api_key == KEY and armed._eraser.project_id == "4242"
    assert isinstance(unarmed, PostHogSink) and unarmed._eraser is None


def test_the_api_host_must_be_an_http_origin() -> None:
    with pytest.raises(ValueError, match="ANALYTICS_API_HOST"):
        settings(analytics_api_host="us.posthog.com")
