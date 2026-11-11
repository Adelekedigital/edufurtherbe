"""Unhandled request errors to PostHog's error tracking, beside Sentry
(decision 339, the side-by-side trial).

**The event is PostHog's `$exception`**, sent through `PostHogSink.capture`,
the batch every analytics event already goes through, so there is one client
and one token path. Its shape follows
https://posthog.com/docs/error-tracking/installation/manual ("Make a request"):
``$exception_list`` of ``type``, ``value``, ``mechanism`` and a ``raw``
``stacktrace`` whose frames carry ``platform: "custom"``, ``lang``,
``function``, ``filename``, ``lineno`` and ``in_app``. Frames are oldest first,
as Sentry orders them.

**Only the exception's type, scrubbed message and frames leave**: no request
or response body, header, cookie, source line or frame local. The whole event
then passes through `observability.scrub`, the one rule for logs and Sentry.

**Identity follows consent.** With a consenting member the event is theirs
(``distinct_id`` = `users.id`, as every server event is); otherwise it is
anonymous, under the request's own random id with
``$process_person_profile: false``
(https://posthog.com/docs/data/anonymous-vs-identified-events).

**Sent after the handler returns, never awaited by it**: `report` schedules
the send as a task that runs the synchronous sink in a thread, with a short
timeout, and swallows any failure; at most `MAX_IN_FLIGHT` at once. The 500
is answered without waiting, and a PostHog outage can neither change nor slow
it. Not after the response: Starlette's error handler runs outside every
middleware, so nothing there runs once the 500 is sent.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import traceback
from typing import Any
from uuid import uuid4

import httpx

from app.core.config import Settings
from app.core.reporting import ErrorContext
from app.domain.analytics_events import GEOIP_DISABLE
from app.domain.ports import AnalyticsSink
from app.infra.clients.posthog import NullAnalyticsSink, analytics_sink_for
from app.infra.http.client import shared
from app.infra.observability import scrub

__all__ = [
    "ERROR_TIMEOUT",
    "MAX_IN_FLIGHT",
    "PostHogErrorReporter",
    "error_reporter_for",
    "exception_event",
]

logger = logging.getLogger(__name__)

#: Short: a report that cannot land in a few seconds is dropped, not retried.
ERROR_TIMEOUT = httpx.Timeout(3.0)
#: A message longer than this is cut: a driver error can quote a whole statement.
MAX_MESSAGE = 1000
#: Sends in flight at once; past it a report is dropped. Each holds a worker
#: thread for up to the timeout, and an outage that fails every request must
#: not take the threads the token check and other adapters run on.
MAX_IN_FLIGHT = 4


def _frame(frame: traceback.FrameSummary) -> dict[str, Any]:
    path = frame.filename.replace("\\", "/")
    # The path from the package root, never the machine's own directories.
    library = "site-packages/" in path
    for marker in ("site-packages/", "/src/"):
        if marker in path:
            path = path.rsplit(marker, 1)[1]
            break
    else:
        path = path.rsplit("/", 1)[-1]
    return {
        "platform": "custom",
        "lang": "python",
        "function": frame.name,
        "filename": path,
        "lineno": frame.lineno,
        "in_app": not library and path.startswith("app/"),
    }


def exception_event(
    exc: BaseException,
    context: ErrorContext,
    *,
    release: str | None,
    environment: str,
) -> dict[str, Any]:
    """The `$exception` event for ``exc``, scrubbed."""
    member = context.member_id
    properties: dict[str, Any] = {
        "$exception_list": [
            {
                "type": type(exc).__name__,
                "value": str(exc)[:MAX_MESSAGE],
                "mechanism": {"handled": False, "synthetic": False},
                "stacktrace": {
                    "type": "raw",
                    "frames": [_frame(f) for f in traceback.extract_tb(exc.__traceback__)],
                },
            }
        ],
        "request_id": context.request_id,
        "route": context.route,
        "release": release,
        "environment": environment,
        GEOIP_DISABLE: True,
    }
    if member is None:
        properties["$process_person_profile"] = False
    event: dict[str, Any] = scrub(
        {
            "event": "$exception",
            "distinct_id": str(member) if member else (context.request_id or uuid4().hex),
            "timestamp": dt.datetime.now(dt.UTC).isoformat(),
            "properties": properties,
        }
    )
    return event


class PostHogErrorReporter:
    """Satisfies `domain.ports.ErrorReporter`."""

    def __init__(self, sink: AnalyticsSink, *, release: str | None, environment: str) -> None:
        self._sink = sink
        self._release = release
        self._environment = environment
        #: Held so a running send is not garbage-collected before it finishes.
        self._pending: set[asyncio.Task[None]] = set()

    def report(self, exc: BaseException, context: ErrorContext) -> None:
        if len(self._pending) >= MAX_IN_FLIGHT:
            logger.warning("error report dropped: too many in flight")
            return
        try:
            event = exception_event(
                exc, context, release=self._release, environment=self._environment
            )
            task = asyncio.get_running_loop().create_task(self._send(event))
        except Exception:
            logger.warning("error report not built")
            return
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _send(self, event: dict[str, Any]) -> None:
        try:
            await asyncio.to_thread(self._sink.capture, [event])
        except Exception:
            # A warning, not an error: an error line would itself go to Sentry.
            logger.warning("error report not sent to analytics")

    async def drain(self) -> None:
        """Wait for every send in flight, for the tests."""
        while self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)


def error_reporter_for(settings: Settings) -> PostHogErrorReporter | None:
    """The reporter, or ``None`` unless `ANALYTICS_ERRORS_ENABLED` is on and
    analytics can send (`analytics_sink_for`'s rule)."""
    if not settings.analytics_errors_enabled:
        return None
    sink = analytics_sink_for(settings, client=shared(ERROR_TIMEOUT))
    if isinstance(sink, NullAnalyticsSink):
        return None
    return PostHogErrorReporter(
        sink, release=settings.railway_git_commit_sha, environment=settings.environment
    )
