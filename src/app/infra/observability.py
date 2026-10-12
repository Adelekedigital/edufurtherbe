"""Logs and error reports (ADR 0030).

**Logs are JSON on stdout**, through structlog on top of the standard library,
so every existing `logging.getLogger(__name__)` call — and the `extra=` fields
they carry — comes out structured without being rewritten. Each line carries
the request's `request_id` (bound by `api/request_id.py`).

**Errors go to Sentry when, and only when, `SENTRY_DSN` is set** — production
now, development later (owner, 2026-10-12). No request bodies, cookies, headers
or frame locals are sent, and every event passes through the same `scrub` as
every log line, so an address or a bearer token cannot leave in either.

In `infra` because `sentry_sdk` is a vendor SDK, which only this layer may
import; `main.py` calls the two `configure_*` functions once per app.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping
from typing import Any

import sentry_sdk
import structlog
from sentry_sdk.transport import Transport

from app.core.config import Settings

REDACTED = "[REDACTED]"

#: Keys whose value is never written, whatever it holds.
SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "email",
        "phone",
        "password",
        "api_key",
        "access_token",
        "refresh_token",
        "private_key",
        "dsn",
    }
)
#: Key fragments that mark a value sensitive wherever they appear in the name.
SENSITIVE_PARTS = ("password", "secret", "signature")
#: And a name ending in one of these: `refresh_token`, `meeting_tokens`, not
#: `token_count`.
SENSITIVE_SUFFIXES = ("token", "tokens")

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_BEARER = re.compile(r"(?i)\bbearer\s+[\w\-.~+/]+=*")
#: A query parameter carrying a credential: an OAuth `code` or `state`, a Daily
#: door token `t`, or any `*token*`, `key`, `sig` or `signature`.
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:code|state|t|key|sig|signature|[a-z_]*token[a-z_]*)=)[^&#\s\"']+"
)

#: Marks the handler this module installs, so building a second app replaces it
#: rather than stacking another, and leaves anyone else's handler alone.
_OURS = "_edufurther_observability"


class _Stdout(logging.StreamHandler):  # type: ignore[type-arg]
    """Writes to whatever `sys.stdout` is at the moment of writing, not the one
    it was built with: a test runner that swaps stdout per test would otherwise
    leave this writing to a closed stream."""

    @property
    def stream(self) -> Any:
        return sys.stdout

    @stream.setter
    def stream(self, _value: Any) -> None:
        pass


def _is_sensitive(key: object) -> bool:
    name = str(key).lower()
    return (
        name in SENSITIVE_KEYS
        or any(part in name for part in SENSITIVE_PARTS)
        or name.endswith(SENSITIVE_SUFFIXES)
    )


def scrub(value: Any) -> Any:
    """`value` with every address and bearer token masked, and every
    sensitive key's value replaced. The one rule for logs and Sentry alike."""
    if isinstance(value, str):
        masked = _QUERY_SECRET.sub(rf"\1{REDACTED}", value)
        return _BEARER.sub(f"Bearer {REDACTED}", _EMAIL.sub(REDACTED, masked))
    if isinstance(value, Mapping):
        return {k: REDACTED if _is_sensitive(k) else scrub(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(scrub(item) for item in value)
    if isinstance(value, set | frozenset):
        return [scrub(item) for item in value]
    return value


def _redact(_logger: Any, _name: str, event_dict: Any) -> Any:
    return scrub(event_dict)


def _json_wanted(settings: Settings) -> bool:
    if settings.log_format is not None:
        return settings.log_format == "json"
    return settings.environment not in ("local", "ci")


def configure_logging(settings: Settings) -> None:
    """Route every log record — ours, uvicorn's, a library's — through one
    structlog pipeline to stdout. Safe to call again: it replaces its own
    handler and touches no other."""
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.ExtraAdder(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if _json_wanted(settings)
        else structlog.dev.ConsoleRenderer()
    )
    handler = _Stdout()
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                _redact,
                renderer,
            ],
        )
    )
    setattr(handler, _OURS, True)

    root = logging.getLogger()
    for existing in [h for h in root.handlers if getattr(h, _OURS, False)]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.log_level)
    # Uvicorn's own loggers write through their own handlers; sending them to
    # the root instead puts access lines in the same JSON with the same id.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        named = logging.getLogger(name)
        named.handlers.clear()
        named.propagate = True

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def _before_send(event: Any, _hint: Any) -> Any:
    """Scrub the event, and tag it with the request it came from."""
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    if request_id:
        event.setdefault("tags", {})["request_id"] = request_id
    return scrub(event)


def configure_sentry(settings: Settings, *, transport: type[Transport] | None = None) -> bool:
    """Start error reporting if a DSN is configured. Returns whether it did.

    ``transport`` exists for the tests, which record events instead of sending.
    """
    if settings.sentry_dsn is None:
        return False
    sentry_sdk.init(
        dsn=settings.sentry_dsn.get_secret_value(),
        environment=settings.environment,
        release=settings.railway_git_commit_sha,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        # No user, cookies, headers or bodies; no frame locals, which hold
        # whatever the failing code was holding.
        send_default_pii=False,
        max_request_body_size="never",
        include_local_variables=False,
        before_send=_before_send,
        # Transactions carry request URLs and span descriptions too, the moment
        # tracing is sampled above zero.
        before_send_transaction=_before_send,
        transport=transport,
    )
    return True
