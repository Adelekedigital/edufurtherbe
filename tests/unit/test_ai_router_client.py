"""The AI router adapter (decision 267): the token, the body, and every status branch.

The router is an `httpx.MockTransport`; the token is decoded with the public half
of a keypair made here, as the router would. Time and sleep are injected, so the
retry budget is exercised without waiting for it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from app.core.config import Settings
from app.core.errors import ConfigurationError
from app.domain.writing_assist import AssistCall, AssistResult
from app.infra.clients.ai_router import (
    AUDIENCE,
    BUDGET_SECONDS,
    ISSUER,
    MAX_RETRIES,
    SCOPE,
    SUBJECT,
    AIRouterAssistant,
    NullWritingAssistant,
    load_signing_key,
    writing_assistant_for,
)
from app.main import create_app

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = _KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
PUBLIC_PEM = (
    _KEY.public_key()
    .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    .decode()
)
KEY_ID = "edufurther-platform-test"
BASE = "https://router.example.test/"

CALL = AssistCall(
    task="bio_improve",
    source_data={"role": "mentee", "attempt": 1, "text": "Secret bio words."},
    idempotency_key="k" * 64,
    relation_id="0190aaaa-0000-7000-8000-000000000000",
)

COMPLETED = {
    "request_id": "r1",
    "status": "completed",
    "output": {"text": "A better bio."},
    "model_policy_version": "p1",
    "trace_reference": None,
    "prompt_version": "bio_improve-v1",
}


class Clock:
    """A monotonic clock that only `sleep` and each request move."""

    def __init__(self, per_request: float = 0.1) -> None:
        self.now = 0.0
        self.per_request = per_request
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def adapter(
    respond: Callable[[httpx.Request], httpx.Response], clock: Clock | None = None
) -> tuple[AIRouterAssistant, list[httpx.Request], Clock]:
    seen: list[httpx.Request] = []
    clock = clock or Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        # As a real client would: a reply slower than the attempt's timeout
        # never arrives, and the attempt ends at the timeout.
        timeout = request.extensions["timeout"]["read"]
        if clock.per_request > timeout:
            clock.now += timeout
            raise httpx.ReadTimeout("slow", request=request)
        clock.now += clock.per_request
        return respond(request)

    assistant = AIRouterAssistant(
        base_url=BASE,
        private_key_pem=PRIVATE_PEM,
        key_id=KEY_ID,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=clock.sleep,
        clock=clock,
    )
    return assistant, seen, clock


def answer(status: int = 200, body: Any = None, **headers: str) -> Callable[..., httpx.Response]:
    payload = COMPLETED if body is None and status == 200 else body
    return lambda _request: httpx.Response(status, json=payload, headers=headers)


def problem(status: int, code: str) -> dict[str, object]:
    return {"type": "about:blank", "status": status, "code": code, "retryable": False}


def test_a_completed_answer_is_the_text_and_its_version() -> None:
    assistant, seen, _ = adapter(answer())

    assert assistant.run(CALL) == AssistResult(
        text="A better bio.", prompt_version="bio_improve-v1"
    )
    assert len(seen) == 1
    assert str(seen[0].url) == "https://router.example.test/api/v1/internal/ai/execute"


def test_the_body_is_exactly_what_the_router_accepts() -> None:
    """The router's `ExecuteRequest` forbids extra keys; a prompt is never sent."""
    assistant, seen, _ = adapter(answer())
    assistant.run(CALL)

    body = json.loads(seen[0].content)
    assert body == {
        "product_id": "edufurther_platform",
        "feature_id": "profile_writing_assist",
        "task": "bio_improve",
        "taskrelation_id": CALL.relation_id,
        "idempotency_key": CALL.idempotency_key,
        "source_data": CALL.source_data,
    }
    assert seen[0].headers["Idempotency-Key"] == body["idempotency_key"]


def test_the_token_is_rs256_with_the_registered_claims_and_kid() -> None:
    assistant, seen, _ = adapter(answer())
    assistant.run(CALL)

    token = seen[0].headers["Authorization"].removeprefix("Bearer ")
    header = jwt.get_unverified_header(token)
    claims = jwt.decode(token, PUBLIC_PEM, algorithms=["RS256"], audience=AUDIENCE)

    assert header["alg"] == "RS256"
    assert header["kid"] == KEY_ID
    assert (claims["iss"], claims["sub"], claims["aud"], claims["scope"]) == (
        ISSUER,
        SUBJECT,
        AUDIENCE,
        SCOPE,
    )
    assert claims["iss"] == "edufurther-platform"
    assert claims["sub"] == "edufurther-platform-worker"
    assert claims["aud"] == "edufurther-ai-router"
    assert claims["scope"] == "ai:execute"
    assert 0 < claims["exp"] - claims["iat"] <= 300
    assert claims["nbf"] == claims["iat"]
    assert claims["jti"]


@pytest.mark.parametrize("status", ["review", "budget_exhausted", "provider_unavailable"])
def test_any_other_outcome_is_no_answer_and_is_not_retried(status: str) -> None:
    assistant, seen, _ = adapter(answer(body={**COMPLETED, "status": status, "output": None}))

    assert assistant.run(CALL) is None
    assert len(seen) == 1


@pytest.mark.parametrize("status", ["review", "budget_exhausted", "provider_unavailable"])
def test_output_beside_any_other_outcome_is_still_no_answer(status: str) -> None:
    """Only `completed` has a usable `output`, whatever else the body carries."""
    assistant, _, _ = adapter(answer(body={**COMPLETED, "status": status}))

    assert assistant.run(CALL) is None


@pytest.mark.parametrize(
    "body",
    [
        {**COMPLETED, "output": None},
        {**COMPLETED, "output": {"draft": "wrong key"}},
        {**COMPLETED, "output": {"text": 3}},
        {**COMPLETED, "prompt_version": None},
        ["not", "an", "object"],
    ],
    ids=["no-output", "other-key", "not-text", "no-version", "not-an-object"],
)
def test_a_completed_answer_without_usable_parts_is_no_answer(body: Any) -> None:
    assistant, seen, _ = adapter(answer(body=body))

    assert assistant.run(CALL) is None
    assert len(seen) == 1


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def records() -> Iterator[list[logging.LogRecord]]:
    """What the adapter's own logger emits. On the module logger, not `caplog`:
    the app's JSON logging setup can stop records reaching the root logger
    depending on which tests ran first (see `test_finish_in_thread`)."""
    handler = _Records()
    module_logger = logging.getLogger("app.infra.clients.ai_router")
    previous, disabled = module_logger.level, module_logger.disabled
    module_logger.addHandler(handler)
    module_logger.setLevel(logging.DEBUG)
    # A `dictConfig` run by an earlier test disables loggers that existed
    # before it, this one included; re-enabled for the test only.
    module_logger.disabled = False
    try:
        yield handler.records
    finally:
        module_logger.removeHandler(handler)
        module_logger.setLevel(previous)
        module_logger.disabled = disabled


def test_an_unusable_url_is_no_answer_logged_at_error(records: list[logging.LogRecord]) -> None:
    """A base URL without a scheme fails the same way on every attempt: ours,
    logged at error, never retried."""
    clock = Clock()
    assistant = AIRouterAssistant(
        base_url="router.example.test",
        private_key_pem=PRIVATE_PEM,
        key_id=KEY_ID,
        sleep=clock.sleep,
        clock=clock,
    )

    assert assistant.run(CALL) is None
    assert [r.levelno for r in records] == [logging.ERROR]
    assert clock.sleeps == []


def test_a_key_that_is_not_a_pem_fails_without_echoing_it() -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_signing_key("not-a-key-but-a-secret")

    assert "not-a-key-but-a-secret" not in str(raised.value)
    with pytest.raises(ConfigurationError):
        AIRouterAssistant(base_url=BASE, private_key_pem="garbage", key_id=KEY_ID)


def test_a_pem_with_real_line_breaks_signs() -> None:
    assert "\n" in PRIVATE_PEM
    assistant, seen, _ = adapter(answer())

    assert assistant.run(CALL) is not None
    token = seen[0].headers["Authorization"].removeprefix("Bearer ")
    assert jwt.decode(token, PUBLIC_PEM, algorithms=["RS256"], audience=AUDIENCE)


def test_a_one_line_escaped_pem_from_settings_signs() -> None:
    one_line = PRIVATE_PEM.replace("\n", "\\n")
    configured = settings(
        writing_assist_enabled=True, ai_router_url=BASE, ai_router_private_key=one_line
    )

    assistant = writing_assistant_for(configured)

    assert isinstance(assistant, AIRouterAssistant)


def test_the_app_refuses_to_boot_on_a_bad_key_while_the_assistant_is_on() -> None:
    bad = {"ai_router_url": BASE, "ai_router_private_key": SecretStr("garbage")}

    with pytest.raises(ConfigurationError, match="AI_ROUTER_PRIVATE_KEY"):
        create_app(settings(writing_assist_enabled=True, **bad))
    # Off, nothing reads the key, so nothing refuses.
    assert create_app(settings(**bad)) is not None


def test_an_unreadable_body_is_no_answer() -> None:
    assistant, _, _ = adapter(lambda _r: httpx.Response(200, content=b"<html>"))

    assert assistant.run(CALL) is None


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "IDEMPOTENCY_KEY_MISMATCH"),
        (401, "UNAUTHORIZED"),
        (403, "TASK_NOT_ALLOWED"),
        (422, "SOURCE_DATA_TOO_LARGE"),
        (409, "IDEMPOTENCY_CONFLICT"),
    ],
)
def test_a_configuration_refusal_is_logged_at_error_and_not_retried(
    status: int, code: str, records: list[logging.LogRecord]
) -> None:
    assistant, seen, _ = adapter(answer(status, problem(status, code)))

    assert assistant.run(CALL) is None

    assert len(seen) == 1
    errors = [r for r in records if r.levelno == logging.ERROR]
    assert [(r.status, r.code) for r in errors] == [(status, code)]  # type: ignore[attr-defined]
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in records)
    assert "Secret bio words." not in logged
    assert "PRIVATE KEY" not in logged
    assert "Bearer" not in logged


@pytest.mark.parametrize(
    "first",
    [
        httpx.Response(429, json=problem(429, "RATE_LIMITED")),
        httpx.Response(409, json=problem(409, "REQUEST_IN_PROGRESS")),
        httpx.Response(500, json={}),
        httpx.Response(503, content=b"down"),
    ],
    ids=["429", "in-progress", "500", "503"],
)
def test_a_passing_failure_is_retried_with_the_same_key_and_body(first: httpx.Response) -> None:
    replies = iter([first, httpx.Response(200, json=COMPLETED)])
    assistant, seen, clock = adapter(lambda _r: next(replies))

    assert assistant.run(CALL) is not None
    assert len(seen) == 2
    assert seen[0].content == seen[1].content
    assert seen[0].headers["Idempotency-Key"] == seen[1].headers["Idempotency-Key"]
    tokens = [r.headers["Authorization"] for r in seen]
    jtis = {
        jwt.decode(t[7:], PUBLIC_PEM, algorithms=["RS256"], audience=AUDIENCE)["jti"]
        for t in tokens
    }
    assert len(jtis) == 2, "a retry mints a fresh jti"
    assert len(clock.sleeps) == 1


def test_a_connection_failure_is_retried() -> None:
    replies: list[Any] = [httpx.ConnectError("refused"), httpx.Response(200, json=COMPLETED)]

    def respond(_request: httpx.Request) -> httpx.Response:
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    assistant, seen, _ = adapter(respond)

    assert assistant.run(CALL) is not None
    assert len(seen) == 2


def test_retries_stop_after_two() -> None:
    assistant, seen, clock = adapter(answer(500, {}))

    assert assistant.run(CALL) is None
    assert len(seen) == MAX_RETRIES + 1 == 3
    assert clock.sleeps == [0.5, 1.0]


def test_the_router_retry_after_is_honoured() -> None:
    replies = iter(
        [
            httpx.Response(429, json={}, headers={"Retry-After": "2"}),
            httpx.Response(200, json=COMPLETED),
        ]
    )
    assistant, _, clock = adapter(lambda _r: next(replies))

    assert assistant.run(CALL) is not None
    assert clock.sleeps == [2.0]


def test_no_retry_waits_past_the_budget() -> None:
    """A `Retry-After` longer than what is left ends the call rather than overrunning."""
    assistant, seen, clock = adapter(answer(429, {}, **{"Retry-After": "30"}))

    assert assistant.run(CALL) is None
    assert len(seen) == 1
    assert clock.sleeps == []


def test_a_slow_router_ends_inside_the_budget() -> None:
    """Each attempt gets only what is left: three slow 503s cannot outlast it."""
    clock = Clock(per_request=BUDGET_SECONDS / 2)
    assistant, seen, clock = adapter(answer(503, {}), clock)

    assert assistant.run(CALL) is None
    assert len(seen) == 2
    assert clock.now <= BUDGET_SECONDS


def test_each_attempt_is_given_only_the_budget_left() -> None:
    timeouts: list[float] = []

    def respond(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"]["read"])
        return httpx.Response(500, json={})

    assistant, _, _ = adapter(respond)
    assistant.run(CALL)

    assert timeouts[0] == pytest.approx(BUDGET_SECONDS)
    assert timeouts == sorted(timeouts, reverse=True)
    assert all(t <= BUDGET_SECONDS for t in timeouts)


def test_a_timeout_is_no_answer_and_not_retried() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    assistant, seen, _ = adapter(respond)

    assert assistant.run(CALL) is None
    assert len(seen) == 1


def settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"ai_router_url": BASE, "ai_router_private_key": SecretStr(PRIVATE_PEM)},
        {"writing_assist_enabled": True, "ai_router_private_key": SecretStr(PRIVATE_PEM)},
        {"writing_assist_enabled": True, "ai_router_url": BASE},
    ],
    ids=["nothing", "off", "no-url", "no-key"],
)
def test_off_or_unconfigured_is_the_null_assistant(values: dict[str, object]) -> None:
    assistant = writing_assistant_for(settings(**values))

    assert isinstance(assistant, NullWritingAssistant)
    assert assistant.available is False
    assert assistant.run(CALL) is None


def test_on_and_configured_is_the_router() -> None:
    assistant = writing_assistant_for(
        settings(
            writing_assist_enabled=True,
            ai_router_url=BASE,
            ai_router_private_key=SecretStr(PRIVATE_PEM),
        )
    )

    assert isinstance(assistant, AIRouterAssistant)
    assert assistant.available is True


def test_a_one_line_pem_is_read_with_its_line_breaks() -> None:
    one_line = PRIVATE_PEM.replace("\n", "\\n")

    configured = settings(ai_router_private_key=one_line).ai_router_private_key

    assert configured is not None
    assert configured.get_secret_value() == PRIVATE_PEM
    assert PRIVATE_PEM not in repr(settings(ai_router_private_key=one_line))
