"""Logs come out as JSON with the request's id, nothing personal leaves in a log
line or a Sentry event, and Sentry runs only where a DSN is set (ADR 0030)."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any, ClassVar

import httpx
import pytest
import sentry_sdk
import structlog
from pydantic import SecretStr
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport

from app.api.request_id import request_id_from
from app.core.config import Settings
from app.infra.observability import REDACTED, configure_logging, configure_sentry, scrub
from app.main import create_app


def settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)


@pytest.fixture
def json_logs() -> Iterator[None]:
    configure_logging(settings(log_format="json"))
    yield
    structlog.contextvars.clear_contextvars()


def last_line(out: str) -> dict[str, Any]:
    return json.loads(out.strip().splitlines()[-1])


# --------------------------------------------------------------------------
# Scrubbing, the one rule for logs and Sentry


def test_addresses_and_bearer_tokens_are_masked_inside_text() -> None:
    masked = scrub("sent to amy.o+x@mail.example.ng with Bearer eyJhbGc.def-ghi")

    assert masked == f"sent to {REDACTED} with Bearer {REDACTED}"


def test_sensitive_keys_are_replaced_at_any_depth() -> None:
    event = {
        "request": {"headers": {"Authorization": "Bearer x", "Accept": "json"}},
        "extra": [{"refresh_token": "r", "session_id": "s-1"}],
        "email": "a@b.co",
    }

    assert scrub(event) == {
        "request": {"headers": {"Authorization": REDACTED, "Accept": "json"}},
        "extra": [{"refresh_token": REDACTED, "session_id": "s-1"}],
        "email": REDACTED,
    }


def test_what_is_not_personal_passes_unchanged() -> None:
    assert scrub({"job_name": "settle-sessions", "count": 3}) == {
        "job_name": "settle-sessions",
        "count": 3,
    }


# --------------------------------------------------------------------------
# Log lines


@pytest.mark.usefixtures("json_logs")
def test_a_stdlib_log_line_is_json_with_its_extra_fields_and_request_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every existing `logger.info(..., extra=...)` call keeps working."""
    structlog.contextvars.bind_contextvars(request_id="req-12345678")

    logging.getLogger("app.example").info(
        "reminder for %s not scheduled", "amy@mail.example.ng", extra={"session_id": "s-1"}
    )

    line = last_line(capsys.readouterr().out)
    assert line["event"] == f"reminder for {REDACTED} not scheduled"
    assert line["session_id"] == "s-1"
    assert line["request_id"] == "req-12345678"
    assert line["level"] == "info"
    assert line["logger"] == "app.example"


@pytest.mark.usefixtures("json_logs")
def test_an_exception_is_logged_with_its_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    try:
        raise RuntimeError("boom for a@b.co")
    except RuntimeError:
        logging.getLogger("app.example").exception("unhandled")

    line = last_line(capsys.readouterr().out)
    assert "RuntimeError" in line["exception"]
    assert "a@b.co" not in line["exception"]


def test_info_is_dropped_below_the_configured_level(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(settings(log_format="json", log_level="WARNING"))

    logging.getLogger("app.example").info("quiet")

    assert "quiet" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("values", "json_expected"),
    [
        ({"environment": "local"}, False),
        ({"environment": "ci"}, False),
        ({"environment": "staging"}, True),
        ({"environment": "production"}, True),
        ({"environment": "local", "log_format": "json"}, True),
        ({"environment": "staging", "log_format": "console"}, False),
    ],
)
def test_json_anywhere_deployed_unless_told_otherwise(
    values: dict[str, Any], json_expected: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(settings(**values))

    logging.getLogger("app.example").warning("shape")

    out = capsys.readouterr().out.strip().splitlines()[-1]
    assert out.startswith("{") is json_expected


def test_configuring_again_replaces_rather_than_stacks() -> None:
    configure_logging(settings())
    configure_logging(settings())

    ours = [h for h in logging.getLogger().handlers if getattr(h, "_edufurther_observability", 0)]
    assert len(ours) == 1


# --------------------------------------------------------------------------
# Request ids


def test_a_reasonable_inbound_id_is_kept() -> None:
    assert request_id_from("3f2a9c1e-0b7d-4e55-9a1f-1c2d3e4f5a6b") == (
        "3f2a9c1e-0b7d-4e55-9a1f-1c2d3e4f5a6b"
    )


@pytest.mark.parametrize("raw", [None, "", "short", "has space in it", "x" * 129, "<script>"])
def test_anything_else_is_replaced_with_a_new_id(raw: str | None) -> None:
    replaced = request_id_from(raw)

    assert replaced != raw
    assert len(replaced) == 32
    int(replaced, 16)


@pytest.mark.asyncio
async def test_every_response_carries_its_id_and_cors_exposes_it() -> None:
    app = create_app(settings(cors_origins=["https://app.example.test"]))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        fresh = await client.get("/health", headers={"Origin": "https://app.example.test"})
        echoed = await client.get("/health", headers={"X-Request-ID": "lb-0123456789"})
        missing = await client.get("/no-such-route")

    assert len(fresh.headers["X-Request-ID"]) == 32
    assert "X-Request-ID" in fresh.headers["Access-Control-Expose-Headers"]
    assert echoed.headers["X-Request-ID"] == "lb-0123456789"
    assert missing.status_code == 404
    assert missing.headers["X-Request-ID"]


# --------------------------------------------------------------------------
# Sentry


class Recording(Transport):
    events: ClassVar[list[dict[str, Any]]] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        event = envelope.get_event()
        if event is not None:
            Recording.events.append(event)


@pytest.fixture
def recording() -> Iterator[list[dict[str, Any]]]:
    Recording.events = []
    yield Recording.events
    sentry_sdk.get_client().close()
    sentry_sdk.init()  # no DSN: reporting off again for every later test
    structlog.contextvars.clear_contextvars()


def test_without_a_dsn_sentry_stays_off() -> None:
    assert configure_sentry(settings()) is False
    # As a console variable arrives: a string, cleared to blank.
    assert configure_sentry(settings(sentry_dsn="  ")) is False


def test_an_error_is_reported_scrubbed_and_tagged_with_its_request(
    recording: list[dict[str, Any]],
) -> None:
    dsn = SecretStr("https://public@o0.ingest.sentry.io/1")
    assert configure_sentry(
        settings(sentry_dsn=dsn, environment="production", railway_git_commit_sha="abc123"),
        transport=Recording,
    )
    structlog.contextvars.bind_contextvars(request_id="req-87654321")

    try:
        raise RuntimeError("booking failed for amy@mail.example.ng")
    except RuntimeError as exc:
        sentry_sdk.capture_exception(exc)
    sentry_sdk.flush()

    (event,) = recording
    assert event["environment"] == "production"
    assert event["release"] == "abc123"
    assert event["tags"]["request_id"] == "req-87654321"
    assert "amy@mail.example.ng" not in json.dumps(event)
    assert REDACTED in event["exception"]["values"][0]["value"]


# --------------------------------------------------------------------------
# From the local review


def test_credentials_in_a_query_string_are_masked() -> None:
    url = "/callbacks/google/calendar?code=4/0AbXyz&state=sealed.blob&scope=x"
    door = "https://edu.daily.co/room-1?t=eyJhbGciOi.abc"

    assert scrub(url) == f"/callbacks/google/calendar?code={REDACTED}&state={REDACTED}&scope=x"
    assert scrub(door) == f"https://edu.daily.co/room-1?t={REDACTED}"


def test_token_names_are_redacted_but_counts_of_them_are_not() -> None:
    assert scrub({"meeting_tokens": ["a"], "upstash-signature": "s", "token_count": 3}) == {
        "meeting_tokens": REDACTED,
        "upstash-signature": REDACTED,
        "token_count": 3,
    }


def test_addresses_inside_a_set_are_masked() -> None:
    assert scrub({"seen": {"amy@mail.example.ng"}}) == {"seen": [REDACTED]}


def test_a_cleared_release_variable_is_unset_not_empty() -> None:
    assert settings(railway_git_commit_sha="").railway_git_commit_sha is None
    assert settings(log_format=" ").log_format is None


@pytest.mark.usefixtures("recording")
def test_transactions_are_scrubbed_like_errors() -> None:
    configure_sentry(
        settings(sentry_dsn=SecretStr("https://public@o0.ingest.sentry.io/1")),
        transport=Recording,
    )

    options = sentry_sdk.get_client().options
    assert options["before_send_transaction"] is options["before_send"]


@pytest.mark.asyncio
async def test_an_unhandled_500_keeps_its_request_id(capsys: pytest.CaptureFixture[str]) -> None:
    """Starlette's error handler runs outside every middleware: the id has to
    survive the request's own frame to reach the log line and the header."""
    app = create_app(settings(log_format="json"))

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("database fell over")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        failed = await client.get("/boom", headers={"X-Request-ID": "req-500-abcdef"})

    assert failed.status_code == 500
    assert failed.headers["X-Request-ID"] == "req-500-abcdef"
    logged = [json.loads(line) for line in capsys.readouterr().out.splitlines() if "{" in line]
    unhandled = [line for line in logged if line["event"].startswith("unhandled error")]
    assert unhandled
    assert unhandled[-1]["request_id"] == "req-500-abcdef"
    structlog.contextvars.clear_contextvars()
