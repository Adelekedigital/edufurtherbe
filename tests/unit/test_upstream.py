"""``why()``: what a third party said when it refused."""

from __future__ import annotations

import httpx

from app.infra.http.upstream import LIMIT, why


def test_an_exception_with_no_response_falls_back_to_its_own_text() -> None:
    assert why(ValueError("boom")) == "boom"


def test_a_response_with_a_body_has_it_appended() -> None:
    exc = httpx.HTTPStatusError(
        "404",
        request=httpx.Request("GET", "https://example.test"),
        response=httpx.Response(404, content="not found here"),
    )

    assert why(exc) == "404; upstream said: not found here"


def test_an_explicit_response_overrides_the_exceptions_own() -> None:
    """The exception itself carries none — this is `list()`'s `JSONDecodeError`
    case, where the response exists but isn't reachable through `exc`."""
    response = httpx.Response(200, content="<html>not json</html>")

    assert why(ValueError("Expecting value"), response) == (
        "Expecting value; upstream said: <html>not json</html>"
    )


def test_a_secret_is_cut_out_before_the_body_is_shortened() -> None:
    """The secret straddles the limit: shortening first would keep its first half."""
    echoed = "phx_personal_key"
    body = "x" * (LIMIT - 8) + echoed + " after"
    said = why(ValueError("401"), httpx.Response(401, content=body), secret=echoed)

    assert echoed[:8] not in said
    assert said == "401; upstream said: " + ("x" * (LIMIT - 8) + "[secret] after")[:LIMIT]


def test_an_unread_streamed_response_falls_back_rather_than_raising() -> None:
    """`response.text` raises `ResponseNotRead` on a streamed, unread response —
    `why()` must not let that escape from inside its own error handling."""
    request = httpx.Request("GET", "https://example.test")
    response = httpx.Response(500, request=request, stream=httpx.SyncByteStream())

    assert why(ValueError("boom"), response) == "boom"
