"""The origin lock (#426, decision 304): only the front end's proxy reaches `/api/`."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.api.edge_proxy import DETAIL, HEADER, EdgeProxyMiddleware
from app.api.errors import CONTENT_TYPE
from app.core.config import EDGE_PROXY_SECRET_MIN_LENGTH, Settings, env_key
from app.main import EDGE_PROXY_EXEMPT, create_app

CURRENT = "c" * EDGE_PROXY_SECRET_MIN_LENGTH
NEXT = "n" * 64


@pytest.fixture(autouse=True)
def no_process_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A developer's exported secret must not lock or unlock these apps."""
    for name in ("edge_proxy_secret", "edge_proxy_next_secret"):
        monkeypatch.delenv(env_key(name), raising=False)
    yield


class Recorder:
    """A downstream app that says whether it was entered."""

    def __init__(self) -> None:
        self.entered = False

    async def __call__(self, scope: dict[str, Any], _receive: Any, send: Any) -> None:
        self.entered = True
        if scope["type"] != "http":
            return
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def call(path: str, *, value: str | None = None) -> tuple[httpx.Response, Recorder]:
    app = Recorder()
    gated = EdgeProxyMiddleware(app, secrets=(CURRENT, NEXT), exempt=EDGE_PROXY_EXEMPT)
    headers = {} if value is None else {HEADER: value}
    transport = httpx.ASGITransport(app=gated)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=headers), app


# --------------------------------------------------------------------------
# The middleware


@pytest.mark.asyncio
async def test_an_api_request_without_the_secret_is_refused_before_the_app() -> None:
    response, app = await call("/api/v1/me")

    assert response.status_code == 403
    assert response.headers["content-type"] == CONTENT_TYPE
    assert response.json() == {
        "type": "about:blank",
        "title": "Forbidden",
        "status": 403,
        "detail": DETAIL,
    }
    assert not app.entered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value", ["", "wrong", CURRENT[:-1], CURRENT + "x", CURRENT.upper(), f" {CURRENT}"]
)
async def test_anything_but_an_exact_secret_is_refused(value: str) -> None:
    response, app = await call("/api/v1/me", value=value)

    assert response.status_code == 403
    assert not app.entered


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [CURRENT, NEXT])
async def test_the_current_or_the_next_secret_passes(value: str) -> None:
    response, app = await call("/api/v1/me", value=value)

    assert response.status_code == 200
    assert app.entered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/api/v1/callbacks/daily", "/api/v1/callbacks/", "/api/v1/internal/jobs/settle"]
)
async def test_a_machine_route_passes_without_the_secret(path: str) -> None:
    response, app = await call(path)

    assert response.status_code == 200
    assert app.entered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/api/v1/callbacksX/daily", "/api/v1/callbacks", "/api/v1/internal/jobsets", "/api/"]
)
async def test_an_exempt_prefix_is_matched_whole(path: str) -> None:
    response, _ = await call(path)

    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/health", "/ready", "/docs", "/openapi.json", "/apix"])
async def test_outside_the_api_nothing_is_locked(path: str) -> None:
    response, app = await call(path)

    assert response.status_code == 200
    assert app.entered


@pytest.mark.asyncio
async def test_a_lifespan_scope_passes_through() -> None:
    app = Recorder()
    gated = EdgeProxyMiddleware(app, secrets=(CURRENT,), exempt=())

    await gated({"type": "lifespan"}, None, None)  # type: ignore[arg-type]

    assert app.entered


def test_a_lock_with_no_secret_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one secret"):
        EdgeProxyMiddleware(Recorder(), secrets=(), exempt=())


# --------------------------------------------------------------------------
# Settings


def settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_secret_is_unset_and_locks_nothing(blank: str) -> None:
    configured = settings(edge_proxy_secret=blank, edge_proxy_next_secret=blank)

    assert configured.edge_proxy_secret is None
    assert configured.edge_proxy_secrets() == ()


@pytest.mark.parametrize("name", ["edge_proxy_secret", "edge_proxy_next_secret"])
@pytest.mark.parametrize(
    "value",
    [
        "s" * (EDGE_PROXY_SECRET_MIN_LENGTH - 1),
        # A paste carrying a space or newline would pass a length check and then
        # never equal the header the proxy sends.
        "s" * (EDGE_PROXY_SECRET_MIN_LENGTH - 1) + " ",
        CURRENT + "\n",
        " " + CURRENT,
        "s" * 16 + " " + "s" * 16,
        # Non-ASCII never equals the header's bytes.
        "é" * EDGE_PROXY_SECRET_MIN_LENGTH,
    ],
)
def test_a_weak_or_unmatchable_secret_stops_the_app_at_start_up(name: str, value: str) -> None:
    with pytest.raises(ValidationError, match="at least 32 visible ASCII characters"):
        settings(**{name: value})


def test_the_whole_visible_ascii_range_is_accepted() -> None:
    value = "".join(chr(c) for c in range(0x21, 0x7F))

    assert settings(edge_proxy_secret=value).edge_proxy_secrets() == (value,)


def test_the_accepted_secrets_are_current_then_next() -> None:
    assert settings(
        edge_proxy_secret=CURRENT, edge_proxy_next_secret=NEXT
    ).edge_proxy_secrets() == (
        CURRENT,
        NEXT,
    )
    assert settings(edge_proxy_next_secret=NEXT).edge_proxy_secrets() == (NEXT,)


# --------------------------------------------------------------------------
# The application


async def get(app: Any, path: str, headers: dict[str, str] | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=headers or {})


@pytest.mark.asyncio
async def test_without_a_secret_the_app_is_not_locked() -> None:
    response = await get(create_app(settings()), "/api/v1/me")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_a_locked_app_refuses_with_its_request_id_and_cors_headers() -> None:
    app = create_app(settings(edge_proxy_secret=CURRENT, cors_origins=["https://app.example.test"]))

    refused = await get(app, "/api/v1/me", {"Origin": "https://app.example.test"})
    passed = await get(app, "/api/v1/me", {HEADER: CURRENT})
    health = await get(app, "/health")

    assert refused.status_code == 403
    assert refused.headers["X-Request-ID"]
    assert refused.headers["Access-Control-Allow-Origin"] == "https://app.example.test"
    assert passed.status_code == 401  # through the lock, then no token
    assert health.status_code == 200


@pytest.mark.asyncio
async def test_a_refusal_on_a_per_viewer_path_carries_its_cache_headers() -> None:
    """Inside `PerViewerHeadersMiddleware`, so no cache may keep the 403 for
    other viewers once the secret is fixed."""
    refused = await get(create_app(settings(edge_proxy_secret=CURRENT)), "/api/v1/mentors")

    assert refused.status_code == 403
    assert refused.json()["detail"] == DETAIL
    assert "Authorization" in refused.headers["Vary"]


@pytest.mark.asyncio
async def test_a_locked_app_lets_a_signed_callback_reach_its_own_check() -> None:
    app = create_app(settings(edge_proxy_secret=CURRENT))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/callbacks/daily", content=b"{}")

    # Refused by its own signature check (401), never by the lock (403).
    assert response.status_code == 401


def test_every_route_is_locked_unless_it_is_a_named_machine_route() -> None:
    """Pinned rather than derived: a new route under an exempt prefix, or outside
    `/api/`, changes this list, so adding one is a decision somebody reviews."""
    paths = create_app(settings()).openapi()["paths"]
    lock = EdgeProxyMiddleware(Recorder(), secrets=(CURRENT,), exempt=EDGE_PROXY_EXEMPT)

    unlocked = sorted(
        (method.upper(), path)
        for path, operations in paths.items()
        if not lock.locks(path)
        for method in operations
    )

    assert unlocked == [
        ("GET", "/api/v1/callbacks/google/calendar"),
        ("GET", "/health"),
        ("GET", "/ready"),
        ("POST", "/api/v1/callbacks/bio-drafts"),
        ("POST", "/api/v1/callbacks/daily"),
        ("POST", "/api/v1/callbacks/message-emails"),
        ("POST", "/api/v1/callbacks/reminders"),
        ("POST", "/api/v1/internal/jobs/{job_name}"),
    ]
