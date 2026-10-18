"""The writing assistant over the EduFurther AI Router (decision 267).

Authenticates as the router's integration doc requires: a short-lived RS256 JWT
signed here, per attempt, with this service's own private key. The router only
verifies the public half registered in its `SERVICE_CALLERS`, so no bearer
secret is shared. Mirrors the Scholarship Finder's client, with this repo's
conventions: synchronous `httpx` on the shared client, reached through
`asyncio.to_thread`.

**What is retried and what is not**, per the router's handling table:

- `200` with `completed`: the answer. Any other status (`review`,
  `budget_exhausted`, `provider_unavailable`): no answer, no retry.
- `400`, `401`, `403`, `422`, and `409 IDEMPOTENCY_CONFLICT`: our configuration
  is wrong. Logged at error, never retried; a retry would fail the same way.
- `429`, `409 REQUEST_IN_PROGRESS`, `5xx`, and a connection that fails: up to
  `MAX_RETRIES` more tries with backoff, inside one `BUDGET_SECONDS` budget, with
  the same key and body and a fresh token each time.

Nothing here logs the text sent or returned, the key, or the token.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.types import PrivateKeyTypes
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from app.core.config import Settings
from app.core.errors import ConfigurationError
from app.domain.writing_assist import (
    FEATURE_ID,
    OUTPUT_TEXT_KEY,
    PRODUCT_ID,
    AssistCall,
    AssistResult,
    WritingAssistant,
)
from app.infra.http.client import shared

logger = logging.getLogger(__name__)

__all__ = [
    "AUDIENCE",
    "ISSUER",
    "SCOPE",
    "SUBJECT",
    "AIRouterAssistant",
    "NullWritingAssistant",
    "load_signing_key",
    "writing_assistant_for",
]

#: Registered with the router admin in `SERVICE_CALLERS`; a change here means
#: registering the public key again under the new values.
ISSUER = "edufurther-platform"
SUBJECT = "edufurther-platform-worker"
AUDIENCE = "edufurther-ai-router"
SCOPE = "ai:execute"
#: The router refuses a token living over 300 s; well inside that.
TOKEN_LIFETIME_SECONDS = 120

EXECUTE_PATH = "/api/v1/internal/ai/execute"
#: The whole call, every retry included, as the request path can afford.
BUDGET_SECONDS = 20.0
MAX_RETRIES = 2
#: Doubling from this, or the router's `Retry-After`, never past what is left.
BACKOFF_SECONDS = 0.5
#: An attempt needs at least this much of the budget to be worth starting.
MIN_ATTEMPT_SECONDS = 1.0

_TIMEOUT = httpx.Timeout(BUDGET_SECONDS)
_RETRIED_CONFLICT = "REQUEST_IN_PROGRESS"


def load_signing_key(pem: str) -> PrivateKeyTypes:
    """The PEM private key, parsed, or a `ConfigurationError` that never echoes it.

    Called at boot while the assistant is on, so a malformed key stops the app
    rather than failing every call with a 500 after it was counted.
    """
    try:
        return load_pem_private_key(pem.encode(), password=None)
    except (ValueError, TypeError) as exc:
        raise ConfigurationError("AI_ROUTER_PRIVATE_KEY is not a PEM private key") from exc


class NullWritingAssistant:
    """Off or unconfigured: never available, never calls anything."""

    @property
    def available(self) -> bool:
        return False

    def run(self, call: AssistCall) -> AssistResult | None:  # noqa: ARG002
        return None


class AIRouterAssistant:
    """`WritingAssistant` over `POST /api/v1/internal/ai/execute`."""

    def __init__(
        self,
        *,
        base_url: str,
        private_key_pem: str,
        key_id: str,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}{EXECUTE_PATH}"
        self._key = load_signing_key(private_key_pem)
        self._key_id = key_id
        # Shared (#370); the token travels on each request instead.
        self._client = client or shared(_TIMEOUT)
        self._sleep = sleep
        self._clock = clock

    @property
    def available(self) -> bool:
        return True

    def _token(self) -> str:
        now = datetime.now(UTC)
        claims = {
            "iss": ISSUER,
            "sub": SUBJECT,
            "aud": AUDIENCE,
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(seconds=TOKEN_LIFETIME_SECONDS),
            # Fresh per attempt: the router refuses a `jti` it has seen.
            "jti": str(uuid.uuid4()),
            "scope": SCOPE,
        }
        return jwt.encode(
            claims,
            self._key,  # type: ignore[arg-type]
            algorithm="RS256",
            headers={"kid": self._key_id},
        )

    def run(self, call: AssistCall) -> AssistResult | None:
        body = {
            "product_id": PRODUCT_ID,
            "feature_id": FEATURE_ID,
            "task": call.task,
            "taskrelation_id": call.relation_id,
            "idempotency_key": call.idempotency_key,
            "source_data": call.source_data,
        }
        deadline = self._clock() + BUDGET_SECONDS
        for attempt in range(MAX_RETRIES + 1):
            remaining = deadline - self._clock()
            if remaining < MIN_ATTEMPT_SECONDS:
                break
            try:
                response = self._client.post(
                    self._url,
                    json=body,
                    headers={
                        "Authorization": f"Bearer {self._token()}",
                        "Idempotency-Key": call.idempotency_key,
                    },
                    timeout=remaining,
                )
            except httpx.TimeoutException:
                logger.warning("ai router timed out", extra={"task": call.task})
                return None
            except (httpx.InvalidURL, httpx.UnsupportedProtocol, jwt.PyJWTError) as exc:
                # Ours, and the same on every attempt: a URL or key it cannot use.
                logger.error(
                    "ai router call could not be made",
                    extra={"task": call.task, "error": type(exc).__name__},
                )
                return None
            except httpx.TransportError as exc:
                logger.warning(
                    "ai router unreachable", extra={"task": call.task, "error": type(exc).__name__}
                )
                response = None
            if response is not None:
                if response.status_code == httpx.codes.OK:
                    return _result(response, call.task)
                if not _retryable(response):
                    logger.error(
                        "ai router refused the call",
                        extra={
                            "task": call.task,
                            "status": response.status_code,
                            "code": _code(response),
                        },
                    )
                    return None
            if attempt == MAX_RETRIES:
                break
            pause = _backoff(response, attempt)
            if deadline - self._clock() - pause < MIN_ATTEMPT_SECONDS:
                break
            self._sleep(pause)
        logger.warning("ai router gave no answer in time", extra={"task": call.task})
        return None


def _code(response: httpx.Response) -> str | None:
    """The router's machine-readable `code`, when its problem body has one."""
    try:
        body = response.json()
    except ValueError:
        return None
    code = body.get("code") if isinstance(body, dict) else None
    return code if isinstance(code, str) else None


def _retryable(response: httpx.Response) -> bool:
    status = response.status_code
    if status == httpx.codes.CONFLICT:
        return _code(response) == _RETRIED_CONFLICT
    return status == httpx.codes.TOO_MANY_REQUESTS or status >= httpx.codes.INTERNAL_SERVER_ERROR


def _backoff(response: httpx.Response | None, attempt: int) -> float:
    """The router's `Retry-After` in seconds when it sends one, doubling otherwise."""
    fallback = BACKOFF_SECONDS * 2.0**attempt
    if response is None:
        return fallback
    try:
        return max(0.0, float(response.headers.get("Retry-After", "")))
    except ValueError:
        return fallback


def _result(response: httpx.Response, task: str) -> AssistResult | None:
    """The answer from a `200`, read defensively; only `completed` carries one."""
    try:
        body: Any = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        logger.error("ai router answered an unreadable body", extra={"task": task})
        return None
    status = body.get("status")
    if status != "completed":
        logger.info("ai router gave no result", extra={"task": task, "outcome": status})
        return None
    output = body.get("output")
    text = output.get(OUTPUT_TEXT_KEY) if isinstance(output, dict) else None
    version = body.get("prompt_version")
    if not isinstance(text, str) or not isinstance(version, str):
        logger.error("ai router answered without text or version", extra={"task": task})
        return None
    return AssistResult(text=text, prompt_version=version)


def writing_assistant_for(settings: Settings) -> WritingAssistant:
    """The router when the feature is on and configured, else the null one.
    The one place either is chosen."""
    key = settings.ai_router_private_key
    if not (settings.writing_assist_enabled and settings.ai_router_url and key):
        return NullWritingAssistant()
    return AIRouterAssistant(
        base_url=settings.ai_router_url,
        private_key_pem=key.get_secret_value(),
        key_id=settings.ai_router_key_id,
    )
