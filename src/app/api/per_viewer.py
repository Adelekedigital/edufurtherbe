"""Cache headers for responses the caller's token can change.

`GET /mentors/{handle}` and its `/reviews` answer the mentor themself
differently from everyone else (settled decision #183), so a cache must key
them on `Authorization` and must never share a signed-in copy.

**Middleware rather than a line in each route**, because the routes only see
the success path. A `404` is raised inside a dependency and becomes a Problem
Details response in the exception handler, after the route has gone — so a
header set by the route never reaches it, and a shared cache holding the
anonymous `404` for a hidden profile could serve it to the owner. Here every
response under the prefix gets the headers, errors included.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = ["PER_VIEWER_PREFIXES", "PerViewerHeadersMiddleware"]

#: Paths whose answer depends on who is asking. The discovery list
#: (`/api/v1/mentors`, no trailing slash) is not one of them: it is identical
#: for everybody and keeps its caching.
PER_VIEWER_PREFIXES = ("/api/v1/mentors/",)


def _has_authorization(scope: Scope) -> bool:
    return any(name == b"authorization" for name, _ in scope.get("headers", ()))


class PerViewerHeadersMiddleware:
    """`Vary: Authorization` on every matching response; `private` when a token came.

    Pure ASGI, like `BodyLimitMiddleware`: it only rewrites the start message.
    **`Cache-Control: private` replaces any value a handler set**, because a
    signed-in view must never be stored by a shared cache whatever else a
    handler thought.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope.get("path", "")).startswith(
            PER_VIEWER_PREFIXES
        ):
            await self.app(scope, receive, send)
            return

        signed_in = _has_authorization(scope)

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                # A second `Vary` combines with any existing one, so nothing is
                # lost; a handler's `Cache-Control` is replaced only when a
                # token came.
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if not (signed_in and name.lower() == b"cache-control")
                ]
                headers.append((b"vary", b"Authorization"))
                if signed_in:
                    headers.append((b"cache-control", b"private"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, with_headers)
