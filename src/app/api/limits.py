"""A ceiling on the request body, counted as it arrives.

**Why middleware and not a check inside the endpoint.** FastAPI resolves an
``UploadFile`` parameter by parsing the multipart form, and Starlette's parser
reads the stream to completion and spools each part to a temporary file. All of
that happens *before* the first line of the dependency runs — measured with an
8 MB body, which arrived on disk with ``file.size`` already known. A check
written there refuses a body that has already been received and written.

**Why counted and not read from the header.** The first version of this trusted
``Content-Length``, and a review found the hole: a chunked request carries no
length, so nothing was compared and the body went through in full. Measured
against a real uvicorn server, a 7 MB chunked body reached the route while the
identical body with an honest header was refused with nothing read. A header is
a claim about a body that may not arrive in that shape.

The failure that leaves open is worse away from the uploads than on them. A
chunked ``PATCH`` is JSON, and ``request.body()`` accumulates JSON **in memory** —
so the unbounded case on an ordinary route costs the worker's RAM, where on an
upload it costs its disk.

**A lying header is not the third hole.** It looks like one and is not: uvicorn
frames the body from ``Content-Length`` and hands the application exactly what was
declared. Probed directly — 1,024 declared, 7 MB sent, 1,024 delivered, then the
connection reset. The count still covers it, at no extra cost.

Both checks stay. The declared one refuses before a byte is read, which counting
cannot do; the count holds when the header is absent, which the declared one
cannot. Each is asserted in `tests/unit/test_limits.py` where only it can fire.

An edge limit (``client_max_body_size``, a CDN rule) is the other half of this
rather than a replacement — it belongs to whoever runs the deployment, and this
has to hold when the API is reached directly.
"""

from __future__ import annotations

import json

from starlette import status
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.errors import CONTENT_TYPE

#: The largest body this API will read, for any route.
#:
#: Set from the image limit because uploads are the only thing here that is not
#: small JSON — a route needing more would be a decision, not a default. Held
#: separately from ``MAX_UPLOAD_BYTES`` on purpose: that one is a rule about an
#: image, this one is a rule about a request, and collapsing them would mean a
#: change to either silently moving the other.
MAX_BODY_BYTES = 6 * 1024 * 1024

_REFUSAL = json.dumps(
    {
        "type": "about:blank",
        "title": "Content Too Large",
        "status": status.HTTP_413_CONTENT_TOO_LARGE,
        "detail": "that request body is larger than 6 MB",
    }
).encode()


class _BodyTooLarge(BaseException):
    """Raised inside the wrapped ``receive`` when the running total goes over.

    **``BaseException``, not ``Exception``, and that is load-bearing.** FastAPI
    reads the body inside a ``try`` whose last clause is a bare ``except
    Exception`` converting anything at all into ``400 There was an error parsing
    the body`` (``fastapi/routing.py``). An ``Exception`` subclass never reaches
    this middleware — measured: the first version of this returned 400 for every
    over-long chunked request. Nothing between here and the route catches
    ``BaseException``, so this arrives where it is handled.

    Deliberately not an ``AppError`` either: `errors.py` registers a handler for
    that hierarchy on the application, which sits *inside* this middleware, and it
    would answer 422 where an over-long body is 413.

    It never escapes — ``__call__`` catches it — so nothing outside this module
    sees a ``BaseException`` that a caller would have to know about.
    """


def _declared_length(scope: Scope) -> int | None:
    """``Content-Length`` as a number, or ``None`` when absent or unparseable.

    Read from the raw scope rather than through ``Request`` — building one costs
    nothing useful here and this runs on every request to the API.
    """
    for name, value in scope.get("headers", ()):
        if name == b"content-length":
            text = value.decode("latin-1")
            return int(text) if text.isdigit() else None
    return None


async def _refuse(send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status.HTTP_413_CONTENT_TOO_LARGE,
            "headers": [
                (b"content-type", CONTENT_TYPE.encode()),
                (b"content-length", str(len(_REFUSAL)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _REFUSAL})


class BodyLimitMiddleware:
    """Refuse a request body over ``max_bytes``, by declared length and by count.

    Pure ASGI rather than ``BaseHTTPMiddleware``, which cannot wrap ``receive``
    and therefore cannot count anything — the whole reason this is a class.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Websocket and lifespan are not HTTP requests and have no body to bound.
        # Passing the original callables through matters: a wrapped `receive`
        # here would sit in the path of every websocket frame for no reason.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = _declared_length(scope)
        if declared is not None and declared > self.max_bytes:
            # Refused without reading — the one thing counting cannot do.
            await _refuse(send)
            return

        received = 0
        responding = False

        async def counted(message_receive: Receive = receive) -> Message:
            nonlocal received
            message = await message_receive()
            # **Only `http.request` carries a body.** An `http.disconnect` must
            # reach the application untouched, or a client hanging up mid-upload
            # leaves the worker waiting forever.
            #
            # This condition is defensive, and the mutation batch says so: with
            # it forced true, every test still passes. That is an *equivalent
            # mutant*, not a gap — an http scope yields only these two message
            # types and a disconnect has no `body` key, so counting it adds
            # zero. The guard is here for the message type ASGI adds next, and
            # is deliberately kept despite being unfalsifiable today.
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLarge
            return message

        async def watched(message: Message) -> None:
            nonlocal responding
            if message["type"] == "http.response.start":
                responding = True
            await send(message)

        try:
            await self.app(scope, counted, watched)
        except _BodyTooLarge:
            if responding:
                # Headers are already on the wire. A second `http.response.start`
                # is a protocol error, so there is nothing to send — the server
                # closes the connection, which is the only signal left.
                raise
            await _refuse(send)
