"""Logs and error reports (ADR 0030).

**Logs are JSON on stdout**, through structlog on top of the standard library,
so every existing `logging.getLogger(__name__)` call — and the `extra=` fields
they carry — comes out structured without being rewritten. Each line carries
the request's `request_id` (bound by `api/request_id.py`).

**Errors go to Sentry when, and only when, `SENTRY_DSN` is set** — production
now, development later (owner, 2026-10-12). No request bodies, cookies, headers
or frame locals are sent, and every event passes through the same `scrub` as
every log line, so an address or a bearer token cannot leave in either.
An unhandled request error is also tagged with its route template and, only
with analytics consent, the member's id (decision 339).

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
from app.core.reporting import ErrorContext, current_error

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

#: An address, plain or percent-encoded as a query string carries it (`%40`,
#: and `%2B` for a `+`): an admin search's `q` reaches the access log that way.
_EMAIL = re.compile(r"[\w.+%-]+(?:@|%40)[\w-]+(?:\.[\w-]+)+")
_BEARER = re.compile(r"(?i)\bbearer\s+[\w\-.~+/]+=*")
#: A query parameter carrying a credential: an OAuth `code` or `state`, a Daily
#: door token `t`, a browser's push `endpoint` (decision 312), or any
#: `*token*`, `key`, `sig` or `signature`.
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:code|state|t|endpoint|key|sig|signature|[a-z_]*token[a-z_]*)=)[^&#\s\"']+"
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


#: Where SQLAlchemy starts quoting the statement; its `[parameters: ...]`,
#: which hold the row's values, follow it.
SQL_MARKER = "[SQL:"


#: A value an error message quotes: asyncpg's `'Jane Smith'`, pydantic's
#: `input_value='...'`, a `"..."` name.
_QUOTED = re.compile(r"'[^'\n]*'|\"[^\"\n]*\"")
#: A URL, kept to its scheme and host: the path and query can name a record or
#: carry a credential, and userinfo is a credential.
_URL = re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.-]*)://(?:[^@/\s'\"]*@)?([^/\s?#'\"]+)[^\s'\"]*")
#: Six or more digits in a row: a phone number, an account or card number.
_LONG_NUMBER = re.compile(r"\d{6,}")
QUOTED_VALUE = "'[value]'"
LONG_NUMBER = "[number]"


def without_statement(message: str) -> str:
    """A database error's message cut to its first line, before SQLAlchemy's
    `[SQL:` and everything after it (the statement, its parameters and a
    driver's `DETAIL` line, which quote names and values). Any other message
    is returned unchanged."""
    if SQL_MARKER not in message:
        return message
    return message.split(SQL_MARKER, 1)[0].partition("\n")[0].rstrip()


def reportable_message(message: str) -> str:
    """**The one rule for an exception's text leaving for Sentry or PostHog**
    (decision 339), applied before `scrub`: the SQL statement cut
    (`without_statement`), every quoted literal masked as `'[value]'`, every
    URL reduced to ``scheme://host`` and every run of six or more digits
    masked. The exception's type is untouched and the message keeps its shape,
    so the same fault still groups as one issue."""
    cut = _URL.sub(r"\1://\2", without_statement(message))
    return _LONG_NUMBER.sub(LONG_NUMBER, _QUOTED.sub(QUOTED_VALUE, cut))


def short_path(filename: str) -> tuple[str, bool]:
    """A frame's file from its package root, never the machine's own
    directories, and whether it is ours (`app/...`) rather than a library's."""
    path = filename.replace(chr(92), "/")
    library = "site-packages/" in path
    for marker in ("site-packages/", "/src/"):
        if marker in path:
            path = path.rsplit(marker, 1)[1]
            break
    else:
        if path.startswith("/") or ":" in path:
            path = path.rsplit("/", 1)[-1]
    return path, not library and path.startswith("app/")


#: What a Sentry error event keeps; everything else (request, contexts, extra,
#: breadcrumbs, server_name, modules, an integration's own fields) is dropped.
_EVENT_KEPT = ("event_id", "timestamp", "platform", "level", "logger", "environment", "release")
#: What a transaction drops: it needs its spans and trace context to be one.
_TRANSACTION_DROPPED = ("request", "extra", "breadcrumbs", "server_name", "user", "modules")


def _frames(stacktrace: Any) -> list[dict[str, Any]]:
    frames = []
    for frame in (stacktrace or {}).get("frames") or []:
        path, in_app = short_path(str(frame.get("filename") or frame.get("abs_path") or ""))
        frames.append(
            {
                "filename": path,
                "function": frame.get("function"),
                "lineno": frame.get("lineno"),
                "in_app": in_app,
            }
        )
    return frames


def _allowed(event: Any, context: ErrorContext | None) -> dict[str, Any]:
    """**A Sentry error event built from an allowlist** (decision 339): the
    exception's type and `reportable_message` value with its frames as file,
    function and line; the log line's own text; the tags we set; the level,
    release and environment; ``user.id`` only when the context carries one."""
    kept: dict[str, Any] = {key: event[key] for key in _EVENT_KEPT if key in event}
    values = (event.get("exception") or {}).get("values") or []
    if values:
        kept["exception"] = {
            "values": [
                {
                    "type": value.get("type"),
                    "value": reportable_message(str(value.get("value") or "")),
                    "mechanism": {
                        "type": (value.get("mechanism") or {}).get("type", "generic"),
                        "handled": (value.get("mechanism") or {}).get("handled", True),
                    },
                    "stacktrace": {"frames": _frames(value.get("stacktrace"))},
                }
                for value in values
            ]
        }
    entry = event.get("logentry") or {}
    text = entry.get("formatted") or entry.get("message") or event.get("message")
    if isinstance(text, str):
        kept["logentry"] = {"message": reportable_message(text)}
    tags = {"request_id": (event.get("tags") or {}).get("request_id")}
    if context is not None:
        tags["route"] = context.route
        kept["transaction"] = context.route
        if context.member_id:
            kept["user"] = {"id": str(context.member_id)}
    kept["tags"] = {key: value for key, value in tags.items() if value}
    return kept


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
    """Scrub the event, and tag it with the request it came from.

    An error event is rebuilt from an allowlist (`_allowed`): an unhandled
    request error carries its route template and, only with analytics consent
    from a sendable member, their id: the `ErrorContext` PostHog gets too
    (decision 339). A transaction keeps its spans and loses its request.
    """
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    if request_id:
        event.setdefault("tags", {})["request_id"] = request_id
    if event.get("type") == "transaction":
        for key in _TRANSACTION_DROPPED:
            event.pop(key, None)
        return scrub(event)
    return scrub(_allowed(event, current_error()))


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
        # Zero means no tracing at all (`None`), not tracing sampled at zero.
        traces_sample_rate=settings.sentry_traces_sample_rate or None,
        # **No `sentry-trace` or `baggage` header on any outgoing request.**
        # sentry-sdk attaches them to every request its httpx integration sees
        # unless this list is narrowed (`should_propagate_trace`), whatever the
        # sample rate: PostHog, Loops, Supabase, Daily and Google would each
        # get our trace ids.
        trace_propagation_targets=[],
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
