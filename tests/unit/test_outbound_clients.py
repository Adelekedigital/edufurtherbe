"""No outbound adapter builds an HTTP client per call (#370, part 2).

Every adapter took `client or httpx.Client(...)`, and the one it built had no
owner: nothing closed it, so each booking, accept and email left a connection
pool behind for the garbage collector. Now each process holds one shared client
per timeout, reused by every adapter, which is also what httpx advises.
"""

from __future__ import annotations

import contextlib
import datetime as dt
from typing import Any

import httpx
import pytest

from app.infra.clients import meetings
from app.infra.clients.meetings import DailyRooms, GoogleCalendar
from app.infra.clients.notifications import LoopsNotifier
from app.infra.clients.scheduler import QStashScheduler
from app.infra.clients.templates import LoopsTemplates
from app.infra.http import client as shared_module

FAKE_KEY = "not-a-real-key"


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every `httpx.Client` constructed, each answering from a stub."""
    constructed: list[dict[str, Any]] = []
    real = httpx.Client

    def answer(request: httpx.Request) -> httpx.Response:
        if "userinfo" in str(request.url):
            return httpx.Response(200, json={"email": "mentor@example.test"})
        return httpx.Response(200, json={})

    class Counting(real):  # type: ignore[misc,valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            constructed.append(kwargs)
            kwargs["transport"] = httpx.MockTransport(answer)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", Counting)
    monkeypatch.setattr(shared_module, "_clients", {})
    return constructed


def test_adapters_share_their_clients(built: list[dict[str, Any]]) -> None:
    """**Built three times, each kind, and at most one client per timeout.**
    One per construction was a pool per request that nothing closed."""
    for _ in range(3):
        DailyRooms(FAKE_KEY)
        GoogleCalendar(client_id="c", client_secret=FAKE_KEY, refresh_token=FAKE_KEY)
        LoopsNotifier(FAKE_KEY)
        QStashScheduler(FAKE_KEY, "https://qstash.example.test")
        LoopsTemplates(FAKE_KEY)

    assert len(built) <= len({str(kwargs.get("timeout")) for kwargs in built})
    assert len(built) <= 2


def test_a_helper_called_without_a_client_reuses_the_shared_one(
    built: list[dict[str, Any]],
) -> None:
    """Every module-level helper: each took `client or httpx.Client(...)` too.
    A stubbed answer may be refused, which is fine: only construction counts."""
    window = (dt.datetime(2026, 1, 1, tzinfo=dt.UTC), dt.datetime(2026, 1, 2, tzinfo=dt.UTC))
    for _ in range(3):
        meetings.account_email(token="t")  # noqa: S106
        with contextlib.suppress(Exception):
            meetings.exchange_code(
                code="c", client_id="i", client_secret=FAKE_KEY, redirect_uri="https://r.test"
            )
        with contextlib.suppress(Exception):
            meetings.free_busy(
                client_id="i",
                client_secret=FAKE_KEY,
                refresh_token=FAKE_KEY,
                start=window[0],
                end=window[1],
            )

    assert len(built) <= 1


def test_each_adapter_still_sends_its_own_credentials() -> None:
    """**Credentials travel per request**, now that the client is shared: a
    header set on the client would be every adapter's, including the wrong one."""
    seen: list[httpx.Request] = []
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: (
                seen.append(r) or httpx.Response(200, json={"name": "r", "url": "u", "id": "i"})
            )
        )
    )

    DailyRooms("daily-key", client=client).create(
        name="r",
        opens_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        closes_at=dt.datetime(2026, 1, 1, 1, tzinfo=dt.UTC),
    )

    assert seen[0].headers["Authorization"] == "Bearer daily-key"
    assert str(seen[0].url).startswith(meetings.API_BASE)


def test_the_shared_client_keeps_no_cookies() -> None:
    """**No state carried from one user's call to the next.** One client now
    serves every mentor's Google calls, so a cookie a provider set while
    connecting one calendar would ride along on the next mentor's request.
    Nothing here authenticates by cookie, so the jar keeps nothing."""
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"Set-Cookie": "sid=mentor-a; Path=/"}, json={})

    client = shared_module.shared(httpx.Timeout(1.0))
    client._transport = httpx.MockTransport(answer)  # type: ignore[attr-defined]

    client.get("https://www.googleapis.com/oauth2/v3/userinfo")
    client.get("https://www.googleapis.com/oauth2/v3/userinfo")

    assert "cookie" not in seen[1].headers
