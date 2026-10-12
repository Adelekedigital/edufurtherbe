"""One id per request, on every log line it writes and on its response.

**Pure ASGI rather than `BaseHTTPMiddleware`**, following `limits.py`: it only
binds a context variable and adds a header, and `BaseHTTPMiddleware` would
buffer the body and break the background sends `SendWhatWasQueued` relies on.

**A caller's `X-Request-ID` is kept when it looks like an id** — a load
balancer or the front end can then name the request in its own logs — and
replaced when it does not, so nobody can write arbitrary text into ours.
"""

from __future__ import annotations

import re
from uuid import uuid4

import structlog
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

HEADER = "X-Request-ID"

#: What an inbound id must look like to be kept: a uuid, a hex string, or any
#: other short token of safe characters.
_ACCEPTABLE = re.compile(r"[A-Za-z0-9._-]{8,128}")


def request_id_from(raw: str | None) -> str:
    """The caller's id when acceptable, otherwise a new one."""
    if raw and _ACCEPTABLE.fullmatch(raw):
        return raw
    return uuid4().hex


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        request_id = request_id_from(headers.get(b"x-request-id", b"").decode("latin-1"))

        async def stamped(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[HEADER] = request_id
            await send(message)

        bound = structlog.contextvars.bind_contextvars(request_id=request_id)
        # **Left bound when the request raises.** Starlette's error handler sits
        # outside every middleware, so the 500's log line, its Sentry event and
        # its `X-Request-ID` (set by `errors.handle_unexpected_error`) all read
        # the id after this frame has gone. Each request runs in its own task,
        # so nothing leaks into the next one.
        await self.app(scope, receive, stamped)
        structlog.contextvars.reset_contextvars(**bound)
