"""Unhandled errors go to Sentry and PostHog alike, with the member's id only
under analytics consent (decision 339)."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import pytest
import sentry_sdk
from fastapi import APIRouter, Depends, Request
from pydantic import SecretStr
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sqlalchemy.exc import ProgrammingError
from tests.posthog_fakes import recording_client

from app import main
from app.api.deps import core as deps
from app.api.deps.core import analytics_consent_of
from app.api.errors import UNMATCHED_ROUTE
from app.core.config import Settings
from app.core.errors import AppError, ConfigurationError, NotFoundError
from app.core.reporting import ErrorContext, member_on, remember_member
from app.infra.clients.posthog import PostHogSink
from app.infra.clients.posthog_errors import (
    ERROR_TIMEOUT,
    MAX_CHAIN,
    MAX_FRAMES,
    MAX_IN_FLIGHT,
    PostHogErrorReporter,
    error_reporter_for,
    exception_event,
)
from app.infra.observability import (
    REDACTED,
    configure_sentry,
    reportable_message,
    without_statement,
)
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
    """Stands in for the auth dependency: what `_resolve` records. `internal`
    is an accepted answer from someone analytics may not name."""
    if consent != "none":
        remember_member(
            request.state,
            MEMBER,
            granted=consent in ("accepted", "internal"),
            sendable=consent != "internal",
        )


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
    ("consent", "expected"),
    [("none", None), ("declined", None), ("internal", None), ("accepted", MEMBER)],
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
    assert event["distinct_id"] != "req-anon-123"
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
    admin = {"id": MEMBER, "analytics_consent_granted": True, "analytics_sendable": True}
    previewed = {"id": UUID(THING), "analytics_consent_granted": True, "analytics_sendable": True}

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
    assert exception["value"] == "(builtins.Exception) relation '[value]' does not exist"
    assert "relation '[value]' does not exist" in exception["value"]
    assert sentry_event["exception"]["values"][-1]["value"] == exception["value"]


async def test_any_other_message_is_left_whole() -> None:
    message = "two lines\nboth kept for amy@mail.example.ng"

    assert without_statement(message) == message
    context = ErrorContext(request_id="req-whole-123", route="/x", member_id=None)
    event = exception_event(RuntimeError(message), context, release=None, environment="ci")
    (exception,) = event["properties"]["$exception_list"]
    assert exception["value"] == f"two lines\nboth kept for {REDACTED}"


# --------------------------------------------------------------------------
# From the local review (decision 339)


class UnmappedError(AppError):
    """An `AppError` no status is mapped for: a 500."""


def recorded_reporter() -> tuple[PostHogErrorReporter, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    sink = PostHogSink(host=HOST, project_token=TOKEN, client=recording_client(seen))
    return PostHogErrorReporter(sink, release="abc123", environment="staging"), seen


async def test_an_anonymous_event_never_takes_the_callers_request_id() -> None:
    context = ErrorContext(request_id="chosen-by-the-caller", route="/x", member_id=None)

    first = exception_event(raised(), context, release=None, environment="ci")
    second = exception_event(raised(), context, release=None, environment="ci")

    assert first["distinct_id"] != second["distinct_id"]
    assert len(first["distinct_id"]) == 32
    assert "chosen-by-the-caller" not in (first["distinct_id"], second["distinct_id"])
    assert first["properties"]["request_id"] == "chosen-by-the-caller"


@pytest.mark.parametrize(
    ("message", "secret"),
    [
        ("invalid input for query argument $2: 'Jane Smith' (expected str)", "Jane Smith"),
        ("1 validation error\n  input_value='Jane Smith', input_type=str", "Jane Smith"),
        (
            "Client error '404 Not Found' for url "
            "'https://api.example.test/v1/users/42/notes?key=abc&name=jane'",
            "/v1/users/42",
        ),
        ('call "Jane" back on 08031234567 today', "08031234567"),
    ],
)
async def test_values_in_an_exception_message_reach_neither_tool(
    recording: list[dict[str, Any]], message: str, secret: str
) -> None:
    with_sentry()
    reporter, seen = recorded_reporter()
    app = an_app(reporter)

    @app.get("/talks")
    async def talks() -> None:
        raise RuntimeError(message)

    await get(app, "/talks")
    await reporter.drain()
    sentry_sdk.flush()

    (request,) = seen
    (event,) = json.loads(request.content)["batch"]
    (sentry_event,) = recording

    for sent in (json.dumps(event), json.dumps(sentry_event)):
        assert secret not in sent
        assert "jane" not in sent.lower()
    (exception,) = event["properties"]["$exception_list"]
    assert exception["type"] == "RuntimeError"
    assert sentry_event["exception"]["values"][-1]["value"] == exception["value"]


async def test_a_message_keeps_its_shape_once_masked() -> None:
    assert (
        reportable_message("argument $2: 'Jane Smith', see https://x.example.test/a?b=1")
        == "argument $2: '[value]', see https://x.example.test"
    )
    assert reportable_message("call 0803 1234567") == "call 0803 [number]"
    assert reportable_message("division by zero") == "division by zero"


@pytest.mark.parametrize("error", [ConfigurationError("SUPABASE_URL unset"), UnmappedError("odd")])
async def test_a_500_from_a_deliberate_error_is_reported_once_to_each(
    recording: list[dict[str, Any]], error: Exception
) -> None:
    with_sentry()
    reported = Reported()
    app = an_app(reported)

    @app.get("/deliberate/{thing_id}")
    async def deliberate(thing_id: str) -> None:
        del thing_id
        raise error

    response = await get(app, f"/deliberate/{THING}")
    sentry_sdk.flush()

    assert response.status_code == 500
    ((exc, context),) = reported.calls
    assert exc is error
    assert context.route == "/deliberate/{thing_id}"
    (sentry_event,) = recording
    assert sentry_event["tags"]["route"] == "/deliberate/{thing_id}"


async def test_a_sentry_event_keeps_only_its_allowlist(
    recording: list[dict[str, Any]],
) -> None:
    with_sentry()

    def from_an_integration(event: Any, _hint: Any) -> Any:
        event["integration_field"] = "leaks-if-kept"
        event.setdefault("extra", {})["leak"] = "leaks-if-kept"
        return event

    sentry_sdk.get_isolation_scope().add_event_processor(from_an_integration)
    sentry_sdk.get_isolation_scope().set_context("custom", {"leak": "leaks-if-kept"})
    sentry_sdk.add_breadcrumb(message="leaks-if-kept", data={"leak": "leaks-if-kept"})
    app = an_app(Reported())

    await get(app, f"/things/{THING}?consent=accepted&email=jane%40mail.example.ng&t=secret1")
    sentry_sdk.flush()

    (event,) = recording
    sent = json.dumps(event)
    assert "leaks-if-kept" not in sent
    assert THING not in sent
    assert "secret1" not in sent
    assert set(event) <= {
        "event_id",
        "timestamp",
        "platform",
        "level",
        "logger",
        "environment",
        "release",
        "exception",
        "logentry",
        "tags",
        "transaction",
        "user",
    }
    assert "request" not in event
    assert event["user"] == {"id": str(MEMBER)}
    assert set(event["tags"]) == {"request_id", "route"}
    frame = event["exception"]["values"][-1]["stacktrace"]["frames"][-1]
    assert set(frame) == {"filename", "function", "lineno", "in_app"}


async def test_the_posthog_event_keeps_only_its_allowlist() -> None:
    context = ErrorContext(request_id="req-keys-1234", route="/x", member_id=None)

    event = exception_event(raised(), context, release="abc123", environment="ci")

    assert set(event) == {"event", "distinct_id", "timestamp", "properties"}
    assert set(event["properties"]) == {
        "$exception_list",
        "request_id",
        "route",
        "release",
        "environment",
        "$geoip_disable",
        "$process_person_profile",
    }


async def test_a_chain_is_sent_outermost_first_and_frames_are_capped() -> None:
    def deep(n: int) -> None:
        if n:
            deep(n - 1)
        raise KeyError("inner")

    try:
        try:
            deep(MAX_FRAMES + 20)
        except KeyError as inner:
            raise RuntimeError("outer") from inner
    except RuntimeError as outer:
        caught = outer
    context = ErrorContext(request_id="req-chain-123", route="/x", member_id=None)

    event = exception_event(caught, context, release=None, environment="ci")

    outer_entry, inner_entry = event["properties"]["$exception_list"]
    assert (outer_entry["type"], inner_entry["type"]) == ("RuntimeError", "KeyError")
    assert len(inner_entry["stacktrace"]["frames"]) == MAX_FRAMES
    assert inner_entry["stacktrace"]["frames"][-1]["function"] == "deep"


async def test_a_chain_stops_at_its_depth() -> None:
    error: BaseException = ValueError("0")
    for depth in range(1, MAX_CHAIN + 3):
        try:
            raise ValueError(str(depth)) from error
        except ValueError as wrapped:
            error = wrapped
    context = ErrorContext(request_id="req-depth-123", route="/x", member_id=None)

    event = exception_event(error, context, release=None, environment="ci")

    assert len(event["properties"]["$exception_list"]) == MAX_CHAIN


async def test_the_send_is_bounded_at_about_three_seconds_in_all() -> None:
    phases = (ERROR_TIMEOUT.connect, ERROR_TIMEOUT.read, ERROR_TIMEOUT.write, ERROR_TIMEOUT.pool)
    assert all(phase is not None for phase in phases)
    assert sum(phase for phase in phases if phase is not None) <= 3.0


async def test_shutdown_waits_for_a_send_in_flight_within_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[Any] = []

    class Slow:
        def __init__(self, seconds: float) -> None:
            self.seconds = seconds

        def capture(self, events: list[dict[str, Any]]) -> None:
            time.sleep(self.seconds)
            sent.extend(events)

        def delete_person(self, user_id: UUID) -> None:
            del user_id

    monkeypatch.setattr(main, "SHUTDOWN_DEADLINE", 0.5)
    for seconds, expected in ((0.05, 1), (2.0, 0)):
        sent.clear()
        app = create_app(settings())
        app.state.error_reporter = PostHogErrorReporter(
            Slow(seconds), release=None, environment="ci"
        )
        context = ErrorContext(request_id="req-stop-1234", route="/x", member_id=None)
        started = time.monotonic()
        async with app.router.lifespan_context(app):
            app.state.error_reporter.report(raised(), context)
        assert len(sent) == expected
        assert time.monotonic() - started < 1.5


@pytest.mark.usefixtures("recording")
async def test_sentry_attaches_no_trace_headers_to_outgoing_requests() -> None:
    with_sentry()
    seen: list[httpx.Request] = []

    with sentry_sdk.start_transaction(name="probe"):
        recording_client(seen).get("https://vendor.example.test/x")

    (request,) = seen
    assert "sentry-trace" not in request.headers
    assert "baggage" not in request.headers


async def test_a_prefixed_route_is_named_by_its_full_template(
    recording: list[dict[str, Any]],
) -> None:
    with_sentry()
    reported = Reported()
    app = an_app(reported)
    # Nested, as the app's own routers are: here `scope["route"].path` alone
    # is the inner router's `/widgets/{widget_id}`.
    router = APIRouter(prefix="/widgets")

    @router.get("/{widget_id}")
    async def widget(widget_id: str) -> None:
        del widget_id
        raise RuntimeError("widget broke")

    outer = APIRouter(prefix="/api/v9")
    outer.include_router(router)
    app.include_router(outer)

    await get(app, f"/api/v9/widgets/{THING}")
    sentry_sdk.flush()

    ((_, context),) = reported.calls
    assert context.route == "/api/v9/widgets/{widget_id}"
    (event,) = recording
    assert event["transaction"] == "/api/v9/widgets/{widget_id}"
    assert THING not in json.dumps(event)
    integration = sentry_sdk.get_client().get_integration(FastApiIntegration)
    assert integration is not None
    assert integration.transaction_style == "url"


async def test_an_unmatched_route_is_reported_as_other(
    recording: list[dict[str, Any]],
) -> None:
    with_sentry()
    reported = Reported()
    app = an_app(reported)

    async def raw(*_asgi: Any) -> None:
        raise RuntimeError("mounted app broke")

    app.mount("/raw", raw)

    await get(app, f"/raw/{THING}")
    sentry_sdk.flush()

    ((_, context),) = reported.calls
    assert context.route == UNMATCHED_ROUTE == "/(other)"
    (event,) = recording
    assert event["tags"]["route"] == "/(other)"
    assert THING not in json.dumps(event)


async def test_me_reads_the_consent_from_the_callers_row() -> None:
    answered = {
        "analytics_consent_granted": False,
        "analytics_consent_version": "v2",
        "analytics_consent_updated_at": "2026-11-11T09:00:00Z",
    }
    unanswered = dict.fromkeys(answered)

    consent = analytics_consent_of(answered)

    assert (consent.granted, consent.version, consent.updated_at) == (
        False,
        "v2",
        "2026-11-11T09:00:00Z",
    )
    assert analytics_consent_of(unanswered) is None
