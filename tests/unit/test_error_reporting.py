"""Unhandled errors go to Sentry and PostHog alike, with the member's id only
under analytics consent (decision 339)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import pytest
import sentry_sdk
from fastapi import Depends, Request
from pydantic import SecretStr
from sqlalchemy.exc import ProgrammingError
from tests.posthog_fakes import recording_client

from app.api.deps import core as deps
from app.core.config import Settings
from app.core.errors import NotFoundError
from app.core.reporting import ErrorContext, member_on, remember_member
from app.infra.clients.posthog import PostHogSink
from app.infra.clients.posthog_errors import (
    MAX_IN_FLIGHT,
    PostHogErrorReporter,
    error_reporter_for,
    exception_event,
)
from app.infra.observability import REDACTED, configure_sentry, without_statement
from app.main import create_app
from conftest import Recording

pytestmark = pytest.mark.asyncio

MEMBER = UUID("0190a000-0000-7000-8000-00000000abcd")
THING = "0190a000-0000-7000-8000-0000000f00d5"
HOST = "https://ph.example.test"
TOKEN = "phc_public"  # noqa: S105 - a public ingestion token, made up
ANALYTICS = {"analytics_enabled": True, "analytics_host": HOST, "analytics_project_token": TOKEN}


def settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)


class Reported:
    """An `ErrorReporter` that keeps what it was given."""

    def __init__(self) -> None:
        self.calls: list[tuple[BaseException, ErrorContext]] = []

    def report(self, exc: BaseException, context: ErrorContext) -> None:
        self.calls.append((exc, context))


def remembered(request: Request, consent: str = "none") -> None:
    """Stands in for the auth dependency: what `_resolve` records."""
    if consent != "none":
        remember_member(request.state, MEMBER, consented=consent == "accepted")


def an_app(reporter: Any) -> Any:
    app = create_app(settings())
    app.state.error_reporter = reporter

    @app.get("/things/{thing_id}", dependencies=[Depends(remembered)])
    async def boom(thing_id: str) -> None:
        del thing_id  # in the path only: the report must not carry it
        raise RuntimeError("failed for amy@mail.example.ng")

    @app.get("/refused")
    async def refused() -> None:
        raise NotFoundError("no such thing")

    @app.get("/typed")
    async def typed(n: int) -> int:
        return n

    return app


async def get(app: Any, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers={"X-Request-ID": "req-errors-1234"})


def with_sentry() -> None:
    configure_sentry(
        settings(
            sentry_dsn=SecretStr("https://public@o0.ingest.sentry.io/1"),
            environment="staging",
            railway_git_commit_sha="abc123",
        ),
        transport=Recording,
    )


# --------------------------------------------------------------------------
# The flag


async def test_off_by_default_and_off_without_analytics() -> None:
    assert error_reporter_for(settings(**ANALYTICS)) is None
    assert error_reporter_for(settings(analytics_errors_enabled=True)) is None
    assert (
        error_reporter_for(settings(analytics_errors_enabled=True, analytics_enabled=True)) is None
    )


async def test_on_with_analytics_configured_it_reports() -> None:
    reporter = error_reporter_for(
        settings(analytics_errors_enabled=True, environment="staging", **ANALYTICS)
    )
    assert isinstance(reporter, PostHogErrorReporter)
    assert create_app(settings(analytics_errors_enabled=True, **ANALYTICS)).state.error_reporter


# --------------------------------------------------------------------------
# What is reported


async def test_a_500_is_reported_once_to_each_tool_with_the_route_template(
    recording: list[dict[str, Any]],
) -> None:
    with_sentry()
    reported = Reported()

    response = await get(an_app(reported), f"/things/{THING}")
    sentry_sdk.flush()

    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "req-errors-1234"
    ((exc, context),) = reported.calls
    assert isinstance(exc, RuntimeError)
    assert context == ErrorContext(
        request_id="req-errors-1234", route="/things/{thing_id}", member_id=None
    )
    (event,) = recording
    assert event["tags"]["route"] == "/things/{thing_id}"
    assert event["tags"]["request_id"] == "req-errors-1234"
    assert (event["release"], event["environment"]) == ("abc123", "staging")
    assert "user" not in event
    assert "amy@mail.example.ng" not in json.dumps(event)


@pytest.mark.parametrize("path", ["/refused", "/typed?n=x"])
async def test_a_handled_4xx_is_reported_to_neither(
    recording: list[dict[str, Any]], path: str
) -> None:
    with_sentry()
    reported = Reported()

    response = await get(an_app(reported), path)
    sentry_sdk.flush()

    assert response.status_code in (404, 422)
    assert reported.calls == []
    assert recording == []


@pytest.mark.parametrize(
    ("consent", "expected"), [("none", None), ("declined", None), ("accepted", MEMBER)]
)
async def test_the_member_id_rides_only_on_an_accepted_consent(
    recording: list[dict[str, Any]], consent: str, expected: UUID | None
) -> None:
    with_sentry()
    reported = Reported()

    await get(an_app(reported), f"/things/{THING}?consent={consent}")
    sentry_sdk.flush()

    ((_, context),) = reported.calls
    assert context.member_id == expected
    (event,) = recording
    assert event.get("user") == ({"id": str(MEMBER)} if expected else None)


async def test_a_failing_posthog_send_leaves_the_500_unchanged() -> None:
    sink = PostHogSink(
        host=HOST,
        project_token=TOKEN,
        client=recording_client(status=503),
    )
    reporter = PostHogErrorReporter(sink, release=None, environment="ci")

    response = await get(an_app(reporter), f"/things/{THING}")
    await reporter.drain()

    assert response.status_code == 500
    assert response.json() == {
        "type": "about:blank",
        "title": "Internal Server Error",
        "status": 500,
    }


# --------------------------------------------------------------------------
# The PostHog event


def raised() -> RuntimeError:
    try:
        raise RuntimeError(f"failed {THING} for amy@mail.example.ng")
    except RuntimeError as exc:
        return exc


async def test_the_event_is_anonymous_without_a_member() -> None:
    context = ErrorContext(request_id="req-anon-123", route="/things/{thing_id}", member_id=None)

    event = exception_event(raised(), context, release="abc123", environment="production")

    assert event["event"] == "$exception"
    assert event["distinct_id"] == "req-anon-123"
    properties = event["properties"]
    assert properties["$process_person_profile"] is False
    assert properties["route"] == "/things/{thing_id}"
    assert (properties["release"], properties["environment"]) == ("abc123", "production")
    assert properties["request_id"] == "req-anon-123"
    (exception,) = properties["$exception_list"]
    assert exception["type"] == "RuntimeError"
    assert "amy@mail.example.ng" not in json.dumps(event)
    assert REDACTED in exception["value"]
    (frame,) = exception["stacktrace"]["frames"]
    assert frame == {
        "platform": "custom",
        "lang": "python",
        "function": "raised",
        "filename": "test_error_reporting.py",
        "lineno": frame["lineno"],
        "in_app": False,
    }


async def test_the_event_is_the_members_with_consent() -> None:
    context = ErrorContext(request_id="req-mine-123", route="/x", member_id=MEMBER)

    event = exception_event(raised(), context, release=None, environment="ci")

    assert event["distinct_id"] == str(MEMBER)
    assert "$process_person_profile" not in event["properties"]


async def test_the_reporter_sends_one_batch_through_the_sink() -> None:
    seen: list[httpx.Request] = []
    sink = PostHogSink(host=HOST, project_token=TOKEN, client=recording_client(seen))
    reporter = PostHogErrorReporter(sink, release="abc123", environment="ci")

    response = await get(an_app(reporter), f"/things/{THING}?consent=accepted")
    await reporter.drain()

    assert response.status_code == 500
    (request,) = seen
    assert request.url == f"{HOST}/batch/"
    body = json.loads(request.content)
    (event,) = body["batch"]
    assert event["distinct_id"] == str(MEMBER)
    assert event["properties"]["route"] == "/things/{thing_id}"
    assert THING not in request.content.decode()


# --------------------------------------------------------------------------
# Who is recorded, from the auth dependency


async def test_a_preview_records_no_member(monkeypatch: pytest.MonkeyPatch) -> None:
    admin = {"id": MEMBER, "analytics_consented": True}
    previewed = {"id": UUID(THING), "analytics_consented": True}

    async def signed_in(*_args: Any) -> dict[str, Any]:
        return admin

    async def preview(*_args: Any) -> dict[str, Any]:
        return previewed

    monkeypatch.setattr(deps, "_signed_in_user", signed_in)
    monkeypatch.setattr(deps, "_previewed_user", preview)
    in_preview = SimpleNamespace(state=SimpleNamespace())
    own = SimpleNamespace(state=SimpleNamespace())

    assert await deps._resolve(in_preview, None, None, "preview-id") is previewed  # type: ignore[arg-type]
    assert await deps._resolve(own, None, None, None) is admin  # type: ignore[arg-type]

    assert member_on(in_preview.state) is None
    assert member_on(own.state) == MEMBER


async def test_a_burst_past_the_in_flight_bound_is_dropped_not_queued() -> None:
    release = asyncio.Event()
    sent: list[Any] = []

    class Slow:
        def capture(self, events: list[dict[str, Any]]) -> None:
            sent.extend(events)

        def delete_person(self, user_id: UUID) -> None:
            del user_id

    reporter = PostHogErrorReporter(Slow(), release=None, environment="ci")
    original = reporter._send

    async def held(event: dict[str, Any]) -> None:
        await release.wait()
        await original(event)

    reporter._send = held  # type: ignore[method-assign]
    context = ErrorContext(request_id="req-burst-123", route="/x", member_id=None)
    for _ in range(MAX_IN_FLIGHT + 2):
        reporter.report(raised(), context)
    release.set()
    await reporter.drain()

    assert len(sent) == MAX_IN_FLIGHT


# --------------------------------------------------------------------------
# A database error's statement and parameters reach neither tool


def a_database_error() -> ProgrammingError:
    return ProgrammingError(
        "SELECT id FROM users WHERE first_name = $1",
        ("Amaka Obi",),
        # A driver's DETAIL line quotes values too, before the statement.
        Exception('relation "users" does not exist\nDETAIL:  Key (slug)=(amaka-obi) exists.'),
    )


async def test_a_statement_and_its_parameters_reach_neither_tool(
    recording: list[dict[str, Any]],
) -> None:
    with_sentry()
    seen: list[httpx.Request] = []
    sink = PostHogSink(host=HOST, project_token=TOKEN, client=recording_client(seen))
    reporter = PostHogErrorReporter(sink, release=None, environment="ci")
    app = an_app(reporter)

    @app.get("/db")
    async def db() -> None:
        raise a_database_error()

    response = await get(app, "/db")
    await reporter.drain()
    sentry_sdk.flush()

    assert response.status_code == 500
    (request,) = seen
    (event,) = json.loads(request.content)["batch"]
    (sentry_event,) = recording
    for sent in (json.dumps(event), json.dumps(sentry_event)):
        assert "Amaka" not in sent
        assert "amaka-obi" not in sent
        assert "first_name" not in sent
    (exception,) = event["properties"]["$exception_list"]
    assert exception["type"] == "ProgrammingError"
    assert exception["value"] == str(a_database_error()).partition("\n")[0]
    assert 'relation "users" does not exist' in exception["value"]
    assert sentry_event["exception"]["values"][-1]["value"] == exception["value"]


async def test_any_other_message_is_left_whole() -> None:
    message = "two lines\nboth kept for amy@mail.example.ng"

    assert without_statement(message) == message
    context = ErrorContext(request_id="req-whole-123", route="/x", member_id=None)
    event = exception_event(RuntimeError(message), context, release=None, environment="ci")
    (exception,) = event["properties"]["$exception_list"]
    assert exception["value"] == f"two lines\nboth kept for {REDACTED}"
