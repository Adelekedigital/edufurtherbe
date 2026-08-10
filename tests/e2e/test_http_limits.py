"""The request-body ceiling, over real HTTP.

`tests/unit/test_limits.py` drives the middleware directly and answers what it
does to ASGI messages. This file answers a different question: **whether the
bytes on the wire ever become those messages.** A chunked request carries no
`Content-Length`, and no in-process transport produces one.

The requests are written as raw bytes rather than through a client, because a
client normalises exactly what is under test — it supplies a `Content-Length`,
it will not send a body that contradicts one, and it decides on its own whether
to chunk.
"""

from __future__ import annotations

import contextlib
import socket

import pytest

# The application's own constant, not the `PROBLEM_JSON` copy in the root
# conftest: `tests/e2e/conftest.py` shadows it for this package, and asserting
# against the source of truth is the better answer anyway.
from app.api.errors import CONTENT_TYPE as PROBLEM_JSON
from app.api.limits import MAX_BODY_BYTES

BLOCK = b"\x00" * (64 * 1024)
OVER = MAX_BODY_BYTES + (1024 * 1024)
#: Any route will do. The ceiling answers before routing, before authentication
#: and before the database — which is what makes this tier cheap.
PATH = "/api/v1/users/00000000-0000-0000-0000-000000000000/avatar"


def status_line(connection: socket.socket) -> str:
    """Read until the headers end, and return the status line.

    Reads to the blank line rather than to EOF: the server may hold the
    connection open, and reading to EOF would block until a timeout even though
    the answer arrived immediately.
    """
    data = b""
    while b"\r\n\r\n" not in data:
        try:
            piece = connection.recv(4096)
        except OSError:
            break
        if not piece:
            break
        data += piece
    return data.split(b"\r\n", 1)[0].decode(errors="replace") if data else "<no response>"


def headers_of(raw: bytes) -> dict[str, str]:
    lines = raw.split(b"\r\n\r\n", 1)[0].split(b"\r\n")[1:]
    return {
        name.decode().strip().lower(): value.decode().strip()
        for name, _, value in (line.partition(b":") for line in lines)
        if name
    }


def send(server, head: bytes, chunks, *, expect_early_close: bool = True) -> tuple[str, bytes]:
    """Write a request by hand and read the response.

    ``expect_early_close`` matters: when the server refuses mid-body it stops
    reading and answers, and further writes raise. That is the correct outcome,
    not a failure — so it is caught rather than allowed to fail the test, and
    the status line still has to say 413.
    """
    raw = b""
    with server.connect() as connection:
        connection.sendall(head)
        try:
            for chunk in chunks:
                connection.sendall(chunk)
        except OSError:
            if not expect_early_close:
                raise
        else:
            # Already closed by a server that refused mid-body — the normal case
            # here, and not a failure.
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_WR)
        while b"\r\n\r\n" not in raw:
            try:
                piece = connection.recv(4096)
            except OSError:
                break
            if not piece:
                break
            raw += piece
    line = raw.split(b"\r\n", 1)[0].decode(errors="replace") if raw else "<no response>"
    return line, raw


def head_for(*, length: int | None = None, chunked: bool = False, path: str = PATH) -> bytes:
    lines = [f"POST {path} HTTP/1.1", "Host: test", "Content-Type: application/octet-stream"]
    if length is not None:
        lines.append(f"Content-Length: {length}")
    if chunked:
        lines.append("Transfer-Encoding: chunked")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def chunked_body(total: int):
    for _ in range(total // len(BLOCK)):
        yield f"{len(BLOCK):X}\r\n".encode() + BLOCK + b"\r\n"
    yield b"0\r\n\r\n"


def plain_body(total: int):
    for _ in range(total // len(BLOCK)):
        yield BLOCK


# --------------------------------------------------------------------------
# the ceiling
# --------------------------------------------------------------------------


def test_a_declared_over_length_body_is_refused(live_server) -> None:
    """The case the in-process suite already covers, repeated here as the
    control. If this failed, the harness would be at fault rather than the code,
    and every other result in the file would be worthless."""
    line, raw = send(live_server, head_for(length=OVER), plain_body(OVER))

    assert "413" in line, line
    assert headers_of(raw).get("content-type", "").startswith(PROBLEM_JSON)


def test_a_chunked_over_length_body_is_refused(live_probe_server) -> None:
    """**The defect that shipped, and the reason this tier exists.**

    No `Content-Length`, so the header check has nothing to compare. The first
    version of `limits.py` read only the header and let the whole body through;
    every test passed, because `ASGITransport` cannot produce a chunked request.
    """
    line, raw = send(live_probe_server, head_for(chunked=True), chunked_body(OVER))

    assert "413" in line, line
    assert headers_of(raw).get("content-type", "").startswith(PROBLEM_JSON)


def test_a_chunked_body_under_the_ceiling_reaches_the_application(live_server) -> None:
    """Limited, not banned. A ceiling that refused every chunked request would
    pass the test above and break every streaming client."""
    line, _ = send(
        live_server,
        head_for(chunked=True),
        chunked_body(128 * 1024),
        expect_early_close=False,
    )

    # 401: the request reached authorization, which is as far as an untokened
    # request gets. Anything else means it was stopped by the ceiling.
    assert "401" in line, f"a small chunked body was not delivered: {line}"


def test_the_refusal_is_a_status_line_and_not_a_dropped_connection(live_probe_server) -> None:
    """A reset connection is not a 413, and to a client the two are worlds
    apart — one is an answer it can show a user, the other is a network error.

    Named separately because every assertion above would pass if the server
    merely hung up: `"413" in "<no response>"` is false, but only because the
    status line is read. This test exists to say that out loud.
    """
    line, raw = send(live_probe_server, head_for(chunked=True), chunked_body(OVER))

    assert raw, "the server closed the connection without answering"
    assert line.startswith("HTTP/1.1 413"), line


# --------------------------------------------------------------------------
# the server's own framing
# --------------------------------------------------------------------------


def test_a_lying_content_length_delivers_only_what_was_declared(live_server) -> None:
    """**This pins uvicorn's behaviour, not ours.**

    `limits.py` states in prose that a lying `Content-Length` is not a third
    hole, because the server frames the body from the declared length and hands
    the application exactly that. The count covers it anyway — but the reasoning
    in that docstring depends on this being true, and until now nothing checked
    it. If a future server drops the guarantee, this fails and the docstring
    gets revisited instead of quietly becoming wrong.

    Declared 1,024 while sending far more: the excess is not delivered, so the
    ceiling never trips and the request is answered on its merits.
    """
    line, _ = send(live_server, head_for(length=1024), plain_body(OVER))

    assert "413" not in line, (
        "the server delivered more than the declared length, so `limits.py`'s "
        "reasoning about a lying header no longer holds"
    )


def test_a_real_multipart_upload_survives_http_framing(live_server) -> None:
    """A genuine `multipart/form-data` body over the wire, reaching the route.

    Not an upload test — there is no database here and no token. It proves the
    body was framed, parsed and routed, and that the answer came from the
    **authorization rule** rather than from a parse error. A 400 here would mean
    the multipart parser never got a well-formed body.
    """
    boundary = "----e2eprobe"
    payload = (
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="a.jpg"\r\n'
            "Content-Type: image/jpeg\r\n\r\n"
        ).encode()
        + b"\xff\xd8\xff\xe0"
        + f"\r\n--{boundary}--\r\n".encode()
    )

    head = (
        f"POST {PATH} HTTP/1.1\r\nHost: test\r\n"
        f"Content-Type: multipart/form-data; boundary={boundary}\r\n"
        f"Content-Length: {len(payload)}\r\n\r\n"
    ).encode()

    line, _ = send(live_server, head, [payload], expect_early_close=False)

    assert "401" in line, f"answered by something other than authorization: {line}"


# --------------------------------------------------------------------------
# the connection
# --------------------------------------------------------------------------


def test_a_client_that_disappears_mid_body_does_not_wedge_the_server(
    live_server,
) -> None:
    """The failure an in-process transport cannot stage at all: there is no
    connection to drop.

    A worker blocked on a `receive` that will never answer serves nobody, and
    the symptom — everything hangs — looks like a database problem. Proven by a
    **second request on a new connection** rather than by the first one's
    outcome, because the first one is exactly what was abandoned.
    """
    abandoned = live_server.connect()
    abandoned.sendall(head_for(chunked=True))
    abandoned.sendall(f"{len(BLOCK):X}\r\n".encode() + BLOCK + b"\r\n")
    abandoned.close()  # no terminating chunk, no shutdown — just gone

    with live_server.connect() as fresh:
        fresh.sendall(b"GET /health HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n")
        line = status_line(fresh)

    assert "200" in line, f"the server stopped answering after a client vanished: {line}"


@pytest.mark.parametrize("attempt", [1, 2, 3])
def test_the_server_keeps_answering_across_repeated_refusals(
    live_probe_server, attempt: int
) -> None:
    """Three refusals on three connections against one session-scoped server.

    A refusal that left the connection or the server in a bad state would pass
    once and fail the second time, which a single-shot test cannot see.
    """
    line, _ = send(live_probe_server, head_for(chunked=True), chunked_body(OVER))

    assert line.startswith("HTTP/1.1 413"), f"attempt {attempt}: {line}"
