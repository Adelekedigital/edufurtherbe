"""Refuse API requests that did not come through the front end's proxy (#426).

Every browser call reaches Railway through the front end's Vercel rewrite, and
per-caller rate limits live at Vercel's edge (decision 242). Without this lock
anyone could call the Railway URL directly and skip them. The proxy sends a
shared secret in ``X-EduFurther-Proxy-Secret``; a request under ``/api/`` without
a matching value is ``403``. The header name contains "secret", so the log and
Sentry scrubber redacts it by name (`infra/observability.py`).

**Exempt:** everything outside ``/api/`` (`/health`, `/ready`, the docs) and the
machine-called prefixes the caller passes in: QStash, Daily and Google's OAuth
redirect authenticate by signature or a sealed state, and never pass Vercel.

**Two accepted values**, current and next, so the secret rotates without a
window where either side refuses the other. Compared in constant time against
each; never logged. A refusal writes no line of its own: uvicorn's access line
already records it, and a second line per request would let anyone fill the
logs from the public URL.

Pure ASGI, following `limits.py` and `request_id.py`.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Iterable

from starlette import status
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.errors import send_problem

HEADER = "X-EduFurther-Proxy-Secret"
_HEADER_KEY = HEADER.lower().encode("latin-1")

#: What is locked: the API. Health checks and the docs are outside it.
GATED_PREFIX = "/api/"

#: The refusal's `detail`, which a test reads to tell the lock from a route's 403.
DETAIL = "this API is reached through the EduFurther app"

_REFUSAL = json.dumps(
    {
        "type": "about:blank",
        "title": "Forbidden",
        "status": status.HTTP_403_FORBIDDEN,
        "detail": DETAIL,
    }
).encode()


def _header(scope: Scope) -> bytes | None:
    """The first ``X-EduFurther-Proxy-Secret`` value. A second copy cannot help a
    caller: a wrong first value is a refusal, never a pass."""
    for name, value in scope.get("headers", ()):
        if name == _HEADER_KEY:
            return bytes(value)
    return None


class EdgeProxyMiddleware:
    """Pass a request under ``/api/`` only with an accepted proxy secret."""

    def __init__(self, app: ASGIApp, *, secrets: Iterable[str], exempt: Iterable[str]) -> None:
        self.app = app
        # Settings admit visible ASCII only, so this is the header's own byte form.
        self._secrets = tuple(s.encode("ascii") for s in secrets)
        if not self._secrets:
            raise ValueError("EdgeProxyMiddleware needs at least one secret")
        # A prefix always ends in "/", so "/api/v1/callbacks" never exempts
        # "/api/v1/callbacksX".
        self._exempt = tuple(p.rstrip("/") + "/" for p in exempt)

    def locks(self, path: str) -> bool:
        """Whether a request to ``path`` needs the secret."""
        return path.startswith(GATED_PREFIX) and not path.startswith(self._exempt)

    def _accepted(self, value: bytes | None) -> bool:
        if value is None:
            return False
        # Every secret is compared, so the time taken does not say which matched.
        matches = [hmac.compare_digest(value, secret) for secret in self._secrets]
        return any(matches)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.locks(scope["path"]):
            await self.app(scope, receive, send)
            return
        if self._accepted(_header(scope)):
            await self.app(scope, receive, send)
            return
        await send_problem(send, status.HTTP_403_FORBIDDEN, _REFUSAL)
