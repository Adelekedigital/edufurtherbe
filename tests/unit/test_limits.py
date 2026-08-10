"""The request-body ceiling, driven over raw ASGI.

No application, no database, no HTTP client — the middleware is called directly
with a `scope`, a `receive` and a `send`, because the thing under test is what it
does to the *protocol*: which messages it forwards, which it counts, and whether
the application downstream is entered at all.

**That last one is the point.** "Refused with 413" and "refused before a byte was
read" look identical from a response, and only one of them is a limit. The
recording app below reports whether it ran and how much it was handed, so the two
can be told apart.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.api.errors import CONTENT_TYPE
from app.api.limits import MAX_BODY_BYTES, BodyLimitMiddleware


class Recorder:
    """A downstream app that reports what reached it."""

    def __init__(self, *, respond_before_reading: bool = False) -> None:
        self.entered = False
        self.read = 0
        self.saw_disconnect = False
        self.scope_type: str | None = None
        self.respond_before_reading = respond_before_reading
        #: The exact callables handed down, so a test can assert they are the
        #: ones passed in rather than wrappers. "Passed through untouched" is a
        #: statement about identity, and nothing else can check it.
        self.given_receive: Any = None
        self.given_send: Any = None

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        self.entered = True
        self.scope_type = scope["type"]
        self.given_receive = receive
        self.given_send = send

        if scope["type"] != "http":
            # Nothing to read. A websocket or lifespan app does not consume an
            # `http.request`, and pretending otherwise would test the recorder.
            return

        if self.respond_before_reading:
            # A route that answers without waiting for the body — the case where
            # a late refusal cannot be sent because headers are already out.
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"early"})

        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                self.saw_disconnect = True
                return
            self.read += len(message.get("body", b""))
            if not message.get("more_body", False):
                break

        if not self.respond_before_reading:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})


def http_scope(*, length: int | None = None) -> dict[str, Any]:
    headers = [(b"host", b"test")]
    if length is not None:
        headers.append((b"content-length", str(length).encode()))
    return {"type": "http", "method": "POST", "path": "/x", "headers": headers}


def body_messages(total: int, *, chunk: int = 64 * 1024) -> list[dict[str, Any]]:
    """One `http.request` per chunk, `more_body` false on the last."""
    messages = []
    sent = 0
    while sent < total:
        size = min(chunk, total - sent)
        sent += size
        messages.append({"type": "http.request", "body": b"\x00" * size, "more_body": sent < total})
    if not messages:
        messages.append({"type": "http.request", "body": b"", "more_body": False})
    return messages


async def drive(
    app: Recorder,
    scope: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    max_bytes: int | None = None,
) -> tuple[int | None, bytes, str | None]:
    """Run one exchange. Returns (status, body, content-type)."""
    queue = list(messages)
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        # An empty queue means the client has gone quiet, which on the wire is a
        # disconnect rather than a hang.
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    middleware = BodyLimitMiddleware(app, **({} if max_bytes is None else {"max_bytes": max_bytes}))
    await middleware(scope, receive, send)

    start = next((m for m in sent if m["type"] == "http.response.start"), None)
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    header = None
    if start is not None:
        header = next(
            (v.decode() for k, v in start.get("headers", []) if k == b"content-type"), None
        )
    return (None if start is None else start["status"]), body, header


# --------------------------------------------------------------------------
# the declared length — the check that refuses before reading
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_over_length_declaration_is_refused_without_entering_the_app() -> None:
    """The whole reason the declared check is kept. Counting cannot do this:
    by the time a count knows, the bytes have arrived."""
    app = Recorder()

    status, body, content_type = await drive(
        app, http_scope(length=MAX_BODY_BYTES + 1), body_messages(MAX_BODY_BYTES + 1)
    )

    assert status == 413
    assert app.entered is False, "the application ran for a request refused on its length"
    assert app.read == 0
    assert content_type == CONTENT_TYPE
    assert json.loads(body)["status"] == 413


@pytest.mark.asyncio
async def test_a_declaration_at_the_ceiling_is_allowed() -> None:
    """The boundary. `> max` and `>= max` differ by exactly the request a user
    is documented to be allowed to make."""
    app = Recorder()

    status, _, _ = await drive(
        app, http_scope(length=MAX_BODY_BYTES), body_messages(MAX_BODY_BYTES)
    )

    assert status == 200
    assert app.read == MAX_BODY_BYTES


@pytest.mark.asyncio
async def test_a_junk_content_length_is_not_trusted_and_not_fatal() -> None:
    """`Content-Length: banana` is unparseable, so there is nothing to compare.
    It must fall through to the count rather than raise — the count is what
    holds when the header says nothing useful."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/x",
        "headers": [(b"content-length", b"banana")],
    }

    small = Recorder()
    status_small, _, _ = await drive(small, scope, body_messages(1024))
    large = Recorder()
    status_large, _, _ = await drive(large, scope, body_messages(MAX_BODY_BYTES + 1))

    assert status_small == 200
    assert status_large == 413, "an unparseable length skipped the count as well"


# --------------------------------------------------------------------------
# the count — the check that holds when there is no declaration
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_chunked_body_over_the_ceiling_is_refused() -> None:
    """The hole this rewrite exists to close. No `Content-Length`, so the
    declared check has nothing to compare and the body went through in full."""
    app = Recorder()

    status, body, content_type = await drive(app, http_scope(), body_messages(MAX_BODY_BYTES + 1))

    assert status == 413
    assert content_type == CONTENT_TYPE
    assert json.loads(body)["status"] == 413


@pytest.mark.asyncio
async def test_the_count_stops_reading_rather_than_draining_the_body() -> None:
    """Refused **during** the stream, not after it. A count that only decided at
    the end would have accepted every byte first, which is the failure the
    endpoint-level check already had."""
    app = Recorder()

    await drive(app, http_scope(), body_messages(MAX_BODY_BYTES * 4))

    assert app.read <= MAX_BODY_BYTES + 64 * 1024, (
        f"the whole body was read before refusing: {app.read:,} bytes"
    )


@pytest.mark.asyncio
async def test_a_chunked_body_under_the_ceiling_still_works() -> None:
    """Streamed bodies are limited, not banned. Requiring a length everywhere
    would have been the easy fix and would refuse every chunked client."""
    app = Recorder()

    status, _, _ = await drive(app, http_scope(), body_messages(64 * 1024))

    assert status == 200
    assert app.read == 64 * 1024


@pytest.mark.asyncio
async def test_a_body_of_exactly_the_ceiling_passes_the_count() -> None:
    app = Recorder()

    status, _, _ = await drive(app, http_scope(), body_messages(MAX_BODY_BYTES))

    assert status == 200
    assert app.read == MAX_BODY_BYTES


@pytest.mark.asyncio
async def test_one_byte_over_the_ceiling_does_not() -> None:
    """Paired with the test above so the boundary is pinned from both sides."""
    app = Recorder()

    status, _, _ = await drive(app, http_scope(), body_messages(MAX_BODY_BYTES + 1))

    assert status == 413


# --------------------------------------------------------------------------
# the protocol
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_disconnect_reaches_the_application() -> None:
    """A client that hangs up mid-upload. Swallowing this leaves the application
    waiting on a `receive` that will never answer — a hung worker, which is a
    worse outcome than the one the limit prevents."""
    app = Recorder()

    await drive(app, http_scope(), [{"type": "http.disconnect"}])

    assert app.saw_disconnect is True


@pytest.mark.asyncio
async def test_a_disconnect_is_not_counted_as_a_body() -> None:
    """It carries no `body` key. Counting it would be harmless today and wrong
    the moment the message shape changes."""
    app = Recorder()
    messages = [*body_messages(1024), {"type": "http.disconnect"}]

    status, _, _ = await drive(app, http_scope(), messages, max_bytes=2048)

    assert status == 200


@pytest.mark.parametrize("scope_type", ["websocket", "lifespan"])
@pytest.mark.asyncio
async def test_a_non_http_scope_is_passed_through_untouched(scope_type: str) -> None:
    """Not an HTTP request and has no body to bound. Wrapping `receive` here
    would put this middleware in the path of every websocket frame for no reason.

    **Asserted on identity**, because that is what "untouched" means: the
    application must receive the very callables handed in, not wrappers that
    happen to behave the same today.
    """
    app = Recorder()
    receive, send = _never, _noop

    await BodyLimitMiddleware(app)({"type": scope_type, "path": "/x"}, receive, send)

    assert app.entered is True
    assert app.scope_type == scope_type
    assert app.given_receive is receive, "receive was wrapped on a non-http scope"
    assert app.given_send is send, "send was wrapped on a non-http scope"


@pytest.mark.asyncio
async def test_an_http_scope_does_get_wrapped_callables() -> None:
    """The other half of the test above. Identity assertions pass trivially if
    nothing is ever wrapped, so one case has to prove wrapping happens."""
    app = Recorder()

    await drive(app, http_scope(), body_messages(16))

    assert app.given_receive is not None
    assert app.given_receive.__name__ == "counted"
    assert app.given_send.__name__ == "watched"


@pytest.mark.asyncio
async def test_an_overflow_after_the_response_started_is_not_answered_twice() -> None:
    """A route that answers without waiting for the body, while the client keeps
    sending. Headers are already on the wire, so a second `http.response.start`
    would be a protocol error — there is nothing to send, and the exception is
    left to close the connection.

    `BaseException` in the `raises`, because that is what the signal derives
    from: FastAPI converts any `Exception` raised while reading a body into a
    400, so an `Exception` subclass would never have reached the middleware."""
    app = Recorder(respond_before_reading=True)

    # No `match`: an empty pattern always passes, which pytest warns about and
    # which would assert nothing. The type below is the assertion.
    with pytest.raises(BaseException) as raised:
        await drive(app, http_scope(), body_messages(MAX_BODY_BYTES + 1))

    assert type(raised.value).__name__ == "_BodyTooLarge"


async def _never() -> dict[str, Any]:
    raise AssertionError("receive was called on a non-http scope")


async def _noop(message: dict[str, Any]) -> None:  # noqa: ARG001
    """Accepts and discards. The parameter is the ASGI `send` signature, which
    is what makes this a valid stand-in — dropping it would change the shape
    being passed through and the identity assertion would prove nothing."""
    return None
