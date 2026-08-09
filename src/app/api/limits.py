"""A ceiling on the request body, applied before anything reads it.

**Why this is middleware and not a check inside the endpoint.** FastAPI resolves
an ``UploadFile`` parameter by parsing the multipart form, and Starlette's parser
reads the stream to completion and spools each part to a temporary file. All of
that happens *before* the first line of the dependency runs — measured here with
an 8 MB body, which arrived on disk with ``file.size`` already known. So a
``Content-Length`` check written inside the endpoint refuses a body that has
already been received and written, which is not a limit. A mutation found it:
deleting that check changed no test, because the check on the decoded bytes
caught everything it did.

Starlette has no body-size middleware of its own, and an edge limit
(``client_max_body_size``, a CDN rule) is the other half of this rather than a
replacement — it belongs to whoever runs the deployment, and this has to hold
when the API is reached directly.

The header is a claim, so this is not the only limit. It is the cheap one, and
`domain.images` still measures the bytes it was actually given.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request, Response, status
from fastapi.responses import JSONResponse

from app.api.errors import CONTENT_TYPE

#: The largest body this API will read, for any route.
#:
#: Set from the image limit because uploads are the only thing here that is not
#: small JSON — a route needing more would be a decision, not a default. Held
#: separately from ``MAX_UPLOAD_BYTES`` on purpose: that one is a rule about an
#: image, this one is a rule about a request, and collapsing them would mean a
#: change to either silently moving the other.
MAX_BODY_BYTES = 6 * 1024 * 1024


async def limit_request_body(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Refuse an over-long body from its declared length, before it is read."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        # Written by hand rather than raised: an exception here would be handled
        # downstream of the middleware that is refusing, and 413 is the status
        # this case has — not the 422 a malformed image gets.
        return JSONResponse(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            media_type=CONTENT_TYPE,
            content={
                "type": "about:blank",
                "title": "Payload Too Large",
                "status": status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                "detail": "that request body is larger than 6 MB",
            },
        )
    return await call_next(request)
