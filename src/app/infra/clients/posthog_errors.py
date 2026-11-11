"""Unhandled request errors to PostHog's error tracking, beside Sentry
(decision 339, the side-by-side trial).

**The event is PostHog's `$exception`**, sent through `PostHogSink.capture`,
the batch every analytics event already goes through, so there is one client
and one token path. Its shape follows
https://posthog.com/docs/error-tracking/installation/manual ("Make a request"):
``$exception_list`` of ``type``, ``value``, ``mechanism`` and a ``raw``
``stacktrace`` whose frames carry ``platform: "custom"``, ``lang``,
``function``, ``filename``, ``lineno`` and ``in_app``. Ordered as PostHog's
own Python SDK orders them (posthog-python ``exception_utils``): the caught
exception first, then its ``__cause__`` or ``__context__``; frames oldest first.

**Built from an allowlist**: the exception's type, its message through
`observability.reportable_message`, and frames as file, function and line;
then the tags Sentry gets (request id, route, release, environment). No
request or response body, header, cookie, source line or frame local. The
whole event then passes through `observability.scrub`.

**Identity follows consent.** With a consenting, sendable member the event is
theirs (``distinct_id`` = `users.id`, as every server event is); otherwise it
is anonymous, under a fresh random id per event (never the caller-supplied
request id) with ``$process_person_profile: false``
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
from types import FrameType
from typing import Any
from uuid import uuid4

import httpx

from app.core.config import Settings
from app.core.reporting import ErrorContext
from app.domain.analytics_events import GEOIP_DISABLE
from app.domain.ports import AnalyticsSink
from app.infra.clients.posthog import NullAnalyticsSink, analytics_sink_for
from app.infra.http.client import shared
from app.infra.observability import reportable_message, scrub, short_path

__all__ = [
    "ERROR_TIMEOUT",
    "MAX_CHAIN",
    "MAX_FRAMES",
    "MAX_IN_FLIGHT",
    "SEND_BOUND",
    "SHUTDOWN_DEADLINE",
    "PostHogErrorReporter",
    "error_reporter_for",
    "exception_event",
]

logger = logging.getLogger(__name__)

#: Short: a report that cannot land in about three seconds is dropped, not
#: retried. httpx times each phase apart, so the bound is their sum: 0.5 s for
#: a pooled connection, 1 s to connect, 0.5 s to write and 1 s to read the
#: one small reply, 3 s in all.
ERROR_TIMEOUT = httpx.Timeout(connect=1.0, read=1.0, write=0.5, pool=0.5)
#: The longest one send can take: every phase at its limit.
SEND_BOUND = sum(
    phase
    for phase in (
        ERROR_TIMEOUT.connect,
        ERROR_TIMEOUT.read,
        ERROR_TIMEOUT.write,
        ERROR_TIMEOUT.pool,
    )
    if phase is not None
)
#: A message longer than this is cut: a driver error can quote a whole statement.
MAX_MESSAGE = 1000
#: Sends in flight at once; past it a report is dropped. Each holds a worker
#: thread for up to the timeout, and an outage that fails every request must
#: not take the threads the token check and other adapters run on.
MAX_IN_FLIGHT = 4
#: How deep a chain of causes is followed, and how many frames each keeps (the
#: innermost, nearest the raise).
MAX_CHAIN = 3
MAX_FRAMES = 50
#: How long shutdown waits for sends still in flight: one whole send and a
#: second to spare, so a send that started is not cut off; what is still
#: pending then is cancelled and counted.
SHUTDOWN_DEADLINE = SEND_BOUND + 1.0


def _frame(frame: FrameType, lineno: int) -> dict[str, Any]:
    """A frame as file, function, line and module. **Ours (`in_app`) by its
    module, `app.*`**, not by its path, which a vendored or installed copy can
    make look like a library's. Only the module's name is read from the frame,
    never its locals or source."""
    module = frame.f_globals.get("__name__")
    return {
        "platform": "custom",
        "lang": "python",
        "function": frame.f_code.co_name,
        "filename": short_path(frame.f_code.co_filename),
        "module": module,
        "lineno": lineno,
        "in_app": isinstance(module, str) and (module == "app" or module.startswith("app.")),
    }


def _chain(exc: BaseException) -> list[BaseException]:
    """``exc`` and the exceptions behind it, outermost first, as Python
    prints them: ``__cause__``, else ``__context__`` unless suppressed."""
    chain = [exc]
    while len(chain) < MAX_CHAIN:
        current = chain[-1]
        behind = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
        if behind is None or behind in chain:
            break
        chain.append(behind)
    return chain


def _exception(exc: BaseException) -> dict[str, Any]:
    # No source read and no locals: cheap enough for the event loop.
    frames = list(traceback.walk_tb(exc.__traceback__))[-MAX_FRAMES:]
    return {
        "type": type(exc).__name__,
        "module": type(exc).__module__,
        "value": reportable_message(str(exc))[:MAX_MESSAGE],
        "mechanism": {"handled": False, "synthetic": False},
        "stacktrace": {"type": "raw", "frames": [_frame(f, line) for f, line in frames]},
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
        "$exception_list": [_exception(each) for each in _chain(exc)],
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
            # Never the request id when anonymous: a caller may choose it.
            "distinct_id": str(member) if member else uuid4().hex,
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

    async def drain(self, timeout: float | None = None) -> int:
        """Wait for every send in flight, at most ``timeout`` seconds in all
        (the app's shutdown, `SHUTDOWN_DEADLINE`), then cancel what is still
        pending. Returns how many were dropped, and logs the count only."""
        if not self._pending:
            return 0
        _, pending = await asyncio.wait(set(self._pending), timeout=timeout)
        for task in pending:
            task.cancel()
        # Each cancel lands at once: the thread it waited on is abandoned.
        await asyncio.gather(*pending, return_exceptions=True)
        if pending:
            logger.warning("error reports dropped at shutdown", extra={"dropped": len(pending)})
        return len(pending)


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
