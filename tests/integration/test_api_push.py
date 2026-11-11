"""Web Push endpoints: the public key, and a caller's own browsers (decision 312).

Every write and read names the caller, except the one that hands a browser
over: an endpoint is one browser profile, so whoever subscribes it last owns
it, with their keys.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_account_purge import DAY, FakeAdmin, FakeStorage, deleted, purge
from tests.integration.test_api_preview_sessions import app_client
from tests.integration.test_api_preview_swap import (
    assert_read_only,
    blocked,
    live_preview,
    previewing,
)
from tests.integration.test_api_writes import make_user
from tests.unit.test_push_config import SUBJECT, vapid_pair

from app.core.config import Settings
from app.domain.push import MAX_SUBSCRIPTIONS_PER_USER
from conftest import api_token, bearer, build_api_app, client_for

pytestmark = [pytest.mark.db, pytest.mark.anyio]

CONFIG = "/api/v1/push/config"
SUBS = "/api/v1/me/push-subscriptions"
FCM = "https://fcm.googleapis.com/fcm/send/"
P256DH = "B" + "p" * 86
AUTH = "a" * 22


async def a_user(engine: AsyncEngine) -> tuple[UUID, dict[str, str]]:
    auth_id = uuid4()
    user_id = await make_user(engine, auth_id, f"push-{uuid4().hex[:8]}@example.com")
    return user_id, bearer(api_token(auth_id))


def body(endpoint: str, p256dh: str = P256DH, auth: str = AUTH) -> dict[str, Any]:
    return {"endpoint": endpoint, "expirationTime": None, "keys": {"p256dh": p256dh, "auth": auth}}


async def rows(engine: AsyncEngine, **where: Any) -> list[Any]:
    clause = " AND ".join(f"{column} = :{column}" for column in where) or "TRUE"
    async with engine.connect() as conn:
        result = await conn.execute(
            text(f"SELECT user_id, endpoint, p256dh, auth FROM push_subscriptions WHERE {clause}"),  # noqa: S608
            where,
        )
        return list(result)


async def unsubscribe(
    client: httpx.AsyncClient, headers: dict[str, str], endpoint: str
) -> httpx.Response:
    return await client.request("DELETE", SUBS, json={"endpoint": endpoint}, headers=headers)


# --- The public key ----------------------------------------------------------


@pytest.fixture
async def configured_client(db_engine: AsyncEngine) -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
    public, private = vapid_pair()
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, vapid_public_key=public, vapid_private_key=private, vapid_subject=SUBJECT
    )
    async with client_for(build_api_app(db_engine, None, settings)) as client:
        yield client, public


async def test_the_key_is_public_and_cacheable(
    configured_client: tuple[httpx.AsyncClient, str],
) -> None:
    client, public = configured_client

    response = await client.get(CONFIG)

    assert response.status_code == 200, response.text
    assert response.json() == {"vapid_public_key": public}
    assert response.headers["cache-control"] == "public, max-age=300"


async def test_unconfigured_push_is_a_503_with_its_problem_type(
    api_client: httpx.AsyncClient,
) -> None:
    response = await api_client.get(CONFIG)

    assert response.status_code == 503
    assert response.json()["type"] == "/problems/push-unavailable"


# --- Subscribing -------------------------------------------------------------


async def test_a_new_browser_is_201_and_a_repeat_is_200(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await a_user(db_engine)
    endpoint = FCM + uuid4().hex

    first = await api_client.post(SUBS, json=body(endpoint), headers=headers)
    again = await api_client.post(SUBS, json=body(endpoint, auth="b" * 22), headers=headers)

    assert first.status_code == 201, first.text
    assert first.json() == {"subscribed": True}
    assert first.headers["cache-control"] == "private, no-store"
    location = first.headers["location"]
    assert location == f"{SUBS}?endpoint={quote(endpoint, safe='')}"
    assert (await api_client.get(location, headers=headers)).json() == {"subscribed": True}
    assert again.headers["location"] == location
    assert again.status_code == 200, again.text
    assert [(row.user_id, row.auth) for row in await rows(db_engine, endpoint=endpoint)] == [
        (user_id, "b" * 22)
    ]


async def test_another_account_on_the_browser_takes_it_over_with_its_keys(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, first = await a_user(db_engine)
    second_id, second = await a_user(db_engine)
    endpoint = FCM + uuid4().hex
    await api_client.post(SUBS, json=body(endpoint), headers=first)

    taken = await api_client.post(
        SUBS, json=body(endpoint, P256DH[:-1] + "q", "c" * 22), headers=second
    )

    assert taken.status_code == 200, taken.text
    [row] = await rows(db_engine, endpoint=endpoint)
    assert (row.user_id, row.p256dh, row.auth) == (second_id, P256DH[:-1] + "q", "c" * 22)
    status = await api_client.get(SUBS, params={"endpoint": endpoint}, headers=first)
    assert status.json() == {"subscribed": False}


@pytest.mark.isolated_db
async def test_past_the_cap_the_least_recently_subscribed_browser_goes(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await a_user(db_engine)
    _, other = await a_user(db_engine)
    others = FCM + uuid4().hex
    await api_client.post(SUBS, json=body(others), headers=other)
    endpoints = [FCM + uuid4().hex for _ in range(MAX_SUBSCRIPTIONS_PER_USER)]
    for endpoint in endpoints:
        assert (
            await api_client.post(SUBS, json=body(endpoint), headers=headers)
        ).status_code == 201
    # The oldest re-subscribes, so the second oldest is now the least recent.
    await api_client.post(SUBS, json=body(endpoints[0]), headers=headers)

    newest = FCM + uuid4().hex
    await api_client.post(SUBS, json=body(newest), headers=headers)

    kept = {row.endpoint for row in await rows(db_engine, user_id=user_id)}
    assert kept == {*endpoints, newest} - {endpoints[1]}
    assert len(kept) == MAX_SUBSCRIPTIONS_PER_USER
    assert len(await rows(db_engine, endpoint=others)) == 1


@pytest.mark.parametrize(
    ("payload", "pointer"),
    [
        (body("https://169.254.169.254/latest/meta-data"), "/endpoint"),
        (body("https://fcm.googleapis.com.evil.test/x"), "/endpoint"),
        (body("http://fcm.googleapis.com/fcm/send/x"), "/endpoint"),
        (body(FCM + "x", p256dh="not base64!"), "/keys/p256dh"),
        (body(FCM + "x", auth=""), "/keys/auth"),
        ({"endpoint": FCM + "x", "keys": {"p256dh": P256DH}}, "/keys/auth"),
        ({**body(FCM + "x"), "extra": 1}, "/extra"),
    ],
)
async def test_a_bad_subscription_is_a_422_at_its_field_and_writes_nothing(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    payload: dict[str, Any],
    pointer: str,
) -> None:
    user_id, headers = await a_user(db_engine)

    response = await api_client.post(SUBS, json=payload, headers=headers)

    assert response.status_code == 422, response.text
    assert [error["pointer"] for error in response.json()["errors"]] == [pointer]
    assert await rows(db_engine, user_id=user_id) == []


async def test_every_route_but_the_key_needs_a_token(api_client: httpx.AsyncClient) -> None:
    endpoint = FCM + "x"
    for response in (
        await api_client.post(SUBS, json=body(endpoint)),
        await unsubscribe(api_client, {}, endpoint),
        await api_client.get(SUBS, params={"endpoint": endpoint}),
    ):
        assert response.status_code == 401


# --- Unsubscribing and asking ------------------------------------------------


async def test_unsubscribing_is_204_and_only_ever_removes_your_own(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, owner = await a_user(db_engine)
    _, stranger = await a_user(db_engine)
    endpoint = FCM + uuid4().hex
    await api_client.post(SUBS, json=body(endpoint), headers=owner)

    theirs = await unsubscribe(api_client, stranger, endpoint)
    unknown = await unsubscribe(api_client, owner, FCM + uuid4().hex)

    assert (theirs.status_code, unknown.status_code) == (204, 204)
    assert len(await rows(db_engine, endpoint=endpoint)) == 1
    assert (await unsubscribe(api_client, owner, endpoint)).status_code == 204
    assert await rows(db_engine, endpoint=endpoint) == []


async def test_an_endpoint_no_longer_allowed_can_still_be_unsubscribed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await a_user(db_engine)
    stale = "https://retired.push.example/abc"
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth) "
                "VALUES (:u, :e, 'p', 'a')"
            ),
            {"u": user_id, "e": stale},
        )

    assert (await unsubscribe(api_client, headers, stale)).status_code == 204
    assert await rows(db_engine, user_id=user_id) == []


async def test_the_status_reads_only_your_own_rows(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, owner = await a_user(db_engine)
    _, stranger = await a_user(db_engine)
    endpoint = FCM + uuid4().hex
    await api_client.post(SUBS, json=body(endpoint), headers=owner)

    mine = await api_client.get(SUBS, params={"endpoint": endpoint}, headers=owner)
    theirs = await api_client.get(SUBS, params={"endpoint": endpoint}, headers=stranger)

    assert mine.status_code == 200, mine.text
    assert mine.json() == {"subscribed": True}
    assert mine.headers["cache-control"] == "private, no-store"
    assert theirs.json() == {"subscribed": False}


async def test_a_purged_account_loses_its_browsers(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await a_user(db_engine)
    _, other = await a_user(db_engine)
    others = FCM + uuid4().hex
    await api_client.post(SUBS, json=body(FCM + uuid4().hex), headers=headers)
    await api_client.post(SUBS, json=body(others), headers=other)

    await deleted(db_engine, user_id, purge_in=-DAY)
    assert await purge(db_engine, FakeAdmin(), FakeStorage()) == {
        "due": 1,
        "purged": 1,
        "failed": 0,
    }

    assert await rows(db_engine, user_id=user_id) == []
    assert len(await rows(db_engine, endpoint=others)) == 1


@pytest.mark.isolated_db
async def test_in_a_preview_the_status_read_is_refused_and_logged(db_engine: AsyncEngine) -> None:
    async with app_client(db_engine) as client:
        admin, _, preview = await live_preview(client, db_engine)

        refused = await client.get(
            SUBS, params={"endpoint": FCM + "x"}, headers=previewing(admin, preview)
        )
        own = await client.get(SUBS, params={"endpoint": FCM + "x"}, headers=bearer(admin.token))

    assert_read_only(refused)
    assert await blocked(db_engine, preview) == [("GET", SUBS)]
    assert own.json() == {"subscribed": False}


async def test_the_spec_types_every_success_body(api_client: httpx.AsyncClient) -> None:
    paths = (await api_client.get("/openapi.json")).json()["paths"]
    subscribe = paths[SUBS]["post"]["responses"]

    for code in ("200", "201"):
        assert subscribe[code]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/PushSubscriptionStatus"
        }
    assert "push service" not in paths[SUBS]["delete"]["responses"]["422"]["description"]
