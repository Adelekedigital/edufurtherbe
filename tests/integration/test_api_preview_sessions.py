"""Preview sessions and the preview log (decision 309).

Every route is an admin route, so each gets the four authorization cases (no
token, no grant, a revoked grant, a live one), plus the feature switch. Then
ownership (another admin, another sign-in), the lapse rule, and the log.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_admin import make_user

from app.core.config import Settings
from app.domain.preview import PAGE_PATH_MAX_LENGTH
from app.infra.db import preview_store
from app.infra.db.engine import create_session_factory
from app.infra.db.preview_store import Owner, read_statement
from conftest import PROBLEM_JSON, add_user, api_token, bearer, build_api_app, client_for

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

BASE = "/api/v1/admin/preview-sessions"
NO_USER = "/problems/preview-user-not-found"

#: Every route, as (method, path, body); `{id}` is a preview of the caller's.
ROUTES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("POST", "", None),
    ("PATCH", "/{id}", None),
    ("GET", "/{id}", None),
    ("POST", "/{id}/end", {"reason": "exit"}),
    ("POST", "/{id}/page-views", {"path": "/mentor/home"}),
]
ROUTE_IDS = ["start", "switch", "read", "end", "page-view"]


def app_client(engine: AsyncEngine, *, on: bool = True) -> httpx.AsyncClient:
    settings = Settings(_env_file=None, preview_as_user_enabled=on)
    return client_for(build_api_app(engine, None, settings))


@pytest_asyncio.fixture
async def client(db_engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    async with app_client(db_engine) as c:
        yield c


class Admin:
    """An admin with a known identity, so a test can mint a second sign-in."""

    def __init__(self, user_id: UUID, auth_id: UUID) -> None:
        self.id = user_id
        self.auth_id = auth_id
        self.sign_in = uuid4()
        self.token = api_token(auth_id, session_id=str(self.sign_in))

    def another_sign_in(self) -> str:
        return api_token(self.auth_id, session_id=str(uuid4()))


async def an_admin(engine: AsyncEngine, role: str = "limited_access") -> Admin:
    auth_id = uuid4()
    user_id = await make_user(engine, auth_id, f"admin-{auth_id}@example.test", role=role)
    return Admin(user_id, auth_id)


async def a_user(
    engine: AsyncEngine, email: str | None = None, *, role: str = "mentor", deleted: bool = False
) -> UUID:
    async with engine.begin() as conn:
        return await add_user(
            conn,
            email or f"user-{uuid4()}@example.test",
            first_name="Grace",
            last_name="Hopper",
            primary_role=role,
            timezone="UTC",
            deleted_at=await conn.scalar(text("SELECT now()")) if deleted else None,
        )


async def start(client: httpx.AsyncClient, admin: Admin, user: UUID) -> dict[str, Any]:
    response = await client.post(BASE, json={"user_id": str(user)}, headers=bearer(admin.token))
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def events(engine: AsyncEngine, preview: str) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT kind, admin_user_id, target_user_id, path, end_reason "
                "FROM preview_events WHERE session_id = :s ORDER BY created_at, id"
            ),
            {"s": preview},
        )
        return [dict(row) for row in rows.mappings()]


async def age(engine: AsyncEngine, preview: str, **minutes: int) -> None:
    """Move a preview's start and last activity back by these many minutes."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE preview_sessions SET "
                "started_at = started_at - make_interval(mins => :s), "
                "last_active_at = last_active_at - make_interval(mins => :a) WHERE id = :p"
            ),
            {"s": minutes.get("started", 0), "a": minutes.get("active", 0), "p": preview},
        )


async def call(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    *,
    token: str | None,
    preview: str,
    user: UUID,
) -> httpx.Response:
    if body is None:
        body = {"user_id": str(user)} if method != "GET" else None
    return await client.request(
        method,
        BASE + path.replace("{id}", preview),
        json=body,
        headers=bearer(token) if token else {},
    )


# --------------------------------------------------------------------------
# Who may reach it
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "body"), ROUTES, ids=ROUTE_IDS)
async def test_no_token_is_refused(
    client: httpx.AsyncClient, method: str, path: str, body: dict[str, Any] | None
) -> None:
    response = await call(
        client, method, path, body, token=None, preview=str(uuid4()), user=uuid4()
    )

    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path", "body"), ROUTES, ids=ROUTE_IDS)
@pytest.mark.parametrize("revoked", [False, True], ids=["no-grant", "revoked"])
async def test_a_caller_without_a_live_grant_gets_404(
    client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    revoked: bool,
) -> None:
    admin = await an_admin(db_engine)
    user = await a_user(db_engine)
    preview = (await start(client, admin, user))["id"]
    auth_id = uuid4()
    await make_user(
        db_engine,
        auth_id,
        f"nobody-{auth_id}@example.test",
        role="super_admin" if revoked else None,
        revoked=revoked,
    )

    response = await call(
        client, method, path, body, token=api_token(auth_id), preview=preview, user=user
    )

    assert response.status_code == 404
    assert response.json()["type"] == "about:blank"


@pytest.mark.parametrize(("method", "path", "body"), ROUTES, ids=ROUTE_IDS)
@pytest.mark.parametrize("role", ["super_admin", "mentor_approval", "limited_access"])
async def test_every_live_grant_may_preview(
    client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    role: str,
) -> None:
    admin = await an_admin(db_engine, role)
    user = await a_user(db_engine)
    preview = (await start(client, admin, user))["id"]

    response = await call(client, method, path, body, token=admin.token, preview=preview, user=user)

    assert response.status_code in (200, 201, 204), response.text


@pytest.mark.parametrize(("method", "path", "body"), ROUTES, ids=ROUTE_IDS)
@pytest.mark.parametrize("with_token", [True, False], ids=["admin", "anonymous"])
async def test_every_route_is_404_while_previews_are_off(
    db_engine: AsyncEngine,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    with_token: bool,
) -> None:
    admin = await an_admin(db_engine, "super_admin")
    user = await a_user(db_engine)
    async with app_client(db_engine) as on:
        preview = (await start(on, admin, user))["id"]
    async with app_client(db_engine, on=False) as off:
        response = await call(
            off,
            method,
            path,
            body,
            token=admin.token if with_token else None,
            preview=preview,
            user=user,
        )

    assert response.status_code == 404
    assert await events(db_engine, preview) == [
        {
            "kind": "start",
            "admin_user_id": admin.id,
            "target_user_id": user,
            "path": None,
            "end_reason": None,
        }
    ]


async def test_a_token_naming_no_sign_in_owns_no_preview(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    admin = await an_admin(db_engine)
    user = await a_user(db_engine)

    response = await client.post(
        BASE,
        json={"user_id": str(user)},
        headers=bearer(api_token(admin.auth_id, session_id=None)),
    )

    assert response.status_code == 401


# --------------------------------------------------------------------------
# Starting
# --------------------------------------------------------------------------


async def test_a_start_answers_the_session_and_logs_it(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    admin = await an_admin(db_engine, "mentor_approval")
    user = await a_user(db_engine, "grace@example.test", role="mentee")

    response = await client.post(BASE, json={"user_id": str(user)}, headers=bearer(admin.token))

    assert response.status_code == 201, response.text
    body = response.json()
    assert response.headers["Location"] == f"{BASE}/{body['id']}"
    assert body["user"] == {
        "user_id": str(user),
        "email": "grace@example.test",
        "first_name": "Grace",
        "last_name": "Hopper",
        "role": "mentee",
    }
    assert body["ended_at"] is None
    assert body["end_reason"] is None
    assert body["revealed"] is False
    assert body["can_reveal"] is True
    assert datetime.fromisoformat(body["expires_at"]) - datetime.fromisoformat(
        body["started_at"]
    ) == timedelta(hours=2)
    assert await events(db_engine, body["id"]) == [
        {
            "kind": "start",
            "admin_user_id": admin.id,
            "target_user_id": user,
            "path": None,
            "end_reason": None,
        }
    ]


async def test_a_start_by_email_ignores_case(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    admin = await an_admin(db_engine)
    user = await a_user(db_engine, "mixed.case@example.test")

    response = await client.post(
        BASE, json={"email": "  Mixed.Case@Example.TEST "}, headers=bearer(admin.token)
    )

    assert response.status_code == 201, response.text
    assert response.json()["user"]["user_id"] == str(user)


@pytest.mark.parametrize(
    "body", [{}, {"user_id": str(uuid4()), "email": "a@example.test"}, {"other": 1}]
)
async def test_a_start_names_exactly_one_user(
    client: httpx.AsyncClient, db_engine: AsyncEngine, body: dict[str, Any]
) -> None:
    admin = await an_admin(db_engine)

    response = await client.post(BASE, json=body, headers=bearer(admin.token))

    assert response.status_code == 422


@pytest.mark.parametrize("who", ["unknown", "deleted", "admin", "self", "unknown-email"])
async def test_only_a_previewable_user_may_be_previewed(
    client: httpx.AsyncClient, db_engine: AsyncEngine, who: str
) -> None:
    admin = await an_admin(db_engine)
    other_admin = await an_admin(db_engine, "limited_access")
    body = {
        "unknown": {"user_id": str(uuid4())},
        "deleted": {"user_id": str(await a_user(db_engine, deleted=True))},
        "admin": {"user_id": str(other_admin.id)},
        "self": {"user_id": str(admin.id)},
        "unknown-email": {"email": "nobody-here@example.test"},
    }[who]

    response = await client.post(BASE, json=body, headers=bearer(admin.token))

    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM_JSON
    assert response.json()["type"] == NO_USER
    async with db_engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM preview_sessions")) == 0


async def test_a_revoked_admin_is_previewable(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    admin = await an_admin(db_engine)
    former = await make_user(
        db_engine, uuid4(), "former@example.test", role="super_admin", revoked=True
    )

    response = await client.post(BASE, json={"user_id": str(former)}, headers=bearer(admin.token))

    assert response.status_code == 201, response.text


async def test_a_second_start_ends_the_first_in_this_sign_in_only(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    admin = await an_admin(db_engine)
    first_user, second_user = await a_user(db_engine), await a_user(db_engine)
    first = (await start(client, admin, first_user))["id"]
    elsewhere = await client.post(
        BASE, json={"user_id": str(first_user)}, headers=bearer(admin.another_sign_in())
    )
    assert elsewhere.status_code == 201

    second = (await start(client, admin, second_user))["id"]

    ended = (await client.get(f"{BASE}/{first}", headers=bearer(admin.token))).json()
    assert ended["ended_at"] is not None
    assert ended["end_reason"] == "exit"
    assert [e["kind"] for e in await events(db_engine, first)] == ["start", "end"]
    assert (await events(db_engine, first))[1]["end_reason"] == "exit"
    live = (await client.get(f"{BASE}/{second}", headers=bearer(admin.token))).json()
    assert live["ended_at"] is None
    other_sign_in = elsewhere.json()["id"]
    assert [e["kind"] for e in await events(db_engine, other_sign_in)] == ["start"]


async def test_a_start_ends_a_lapsed_preview_as_it_lapsed_not_as_exit(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    admin = await an_admin(db_engine)
    first = (await start(client, admin, await a_user(db_engine)))["id"]
    await age(db_engine, first, started=40, active=31)

    await start(client, admin, await a_user(db_engine))

    body = (await client.get(f"{BASE}/{first}", headers=bearer(admin.token))).json()
    assert body["end_reason"] == "idle"
    assert datetime.fromisoformat(body["ended_at"]) - datetime.fromisoformat(
        body["started_at"]
    ) == timedelta(minutes=39)
    log = await events(db_engine, first)
    assert [(e["kind"], e["end_reason"]) for e in log] == [("start", None), ("end", "idle")]


async def test_two_starts_at_once_in_one_sign_in_queue_and_leave_one_live(
    db_engine: AsyncEngine,
) -> None:
    """The second start waits on the admin's row while the first is uncommitted,
    then ends it, rather than failing on the one-live index."""
    admin = await an_admin(db_engine)
    owner = Owner(admin_id=admin.id, auth_session_id=admin.sign_in)
    first_user, second_user = await a_user(db_engine), await a_user(db_engine)
    sessions = create_session_factory(db_engine)

    async with sessions() as one, sessions() as two:
        await preview_store.start(one, owner, first_user)
        second = asyncio.create_task(preview_store.start(two, owner, second_user))
        await asyncio.sleep(0.5)
        assert not second.done(), "the second start did not wait for the first"
        await one.commit()
        await second
        await two.commit()

    async with db_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT target_user_id, end_reason FROM preview_sessions "
                    "WHERE admin_user_id = :a ORDER BY started_at, id"
                ),
                {"a": admin.id},
            )
        ).all()
    assert [(r.target_user_id, r.end_reason) for r in rows] == [
        (first_user, "exit"),
        (second_user, None),
    ]


# --------------------------------------------------------------------------
# Ownership
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "body"), ROUTES[1:], ids=ROUTE_IDS[1:])
@pytest.mark.parametrize(
    "stranger", ["other-admin", "other-admin-same-sign-in", "other-sign-in", "unknown-id"]
)
async def test_a_preview_is_only_its_owners_in_their_sign_in(
    client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    stranger: str,
) -> None:
    admin = await an_admin(db_engine, "super_admin")
    user, other = await a_user(db_engine), await a_user(db_engine)
    preview = (await start(client, admin, user))["id"]
    token, target = {
        "other-admin": ((await an_admin(db_engine, "super_admin")).token, preview),
        # Even presenting the owner's sign-in id: the admin is in the scope too.
        "other-admin-same-sign-in": (
            api_token(
                (await an_admin(db_engine, "super_admin")).auth_id,
                session_id=str(admin.sign_in),
            ),
            preview,
        ),
        "other-sign-in": (admin.another_sign_in(), preview),
        "unknown-id": (admin.token, str(uuid4())),
    }[stranger]

    response = await call(client, method, path, body, token=token, preview=target, user=other)

    assert response.status_code == 404
    assert response.json()["type"] == "about:blank"
    assert [e["kind"] for e in await events(db_engine, preview)] == ["start"]
    async with db_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT target_user_id, ended_at FROM preview_sessions WHERE id = :p"),
                {"p": preview},
            )
        ).one()
    assert row.target_user_id == user
    assert row.ended_at is None


# --------------------------------------------------------------------------
# Switching, reading, ending, page views
# --------------------------------------------------------------------------


async def test_a_switch_previews_another_user_and_clears_the_reveal(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    admin = await an_admin(db_engine)
    user, other = await a_user(db_engine), await a_user(db_engine, role="mentee")
    preview = await start(client, admin, user)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE preview_sessions SET revealed_at = now(), "
                "target_since = now() - interval '5 minutes' WHERE id = :p"
            ),
            {"p": preview["id"]},
        )
    before = await client.get(f"{BASE}/{preview['id']}", headers=bearer(admin.token))
    assert before.json()["revealed"] is True

    response = await client.patch(
        f"{BASE}/{preview['id']}", json={"user_id": str(other)}, headers=bearer(admin.token)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"]["user_id"] == str(other)
    assert body["user"]["role"] == "mentee"
    assert body["revealed"] is False
    assert body["started_at"] == preview["started_at"]
    log = await events(db_engine, preview["id"])
    assert [(e["kind"], e["target_user_id"]) for e in log] == [("start", user), ("switch", other)]
    async with db_engine.connect() as conn:
        moved = await conn.scalar(
            text("SELECT target_since > now() - interval '1 minute' FROM preview_sessions")
        )
    assert moved is True


async def test_a_switch_to_someone_unpreviewable_is_the_typed_404(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    admin = await an_admin(db_engine)
    preview = (await start(client, admin, await a_user(db_engine)))["id"]

    response = await client.patch(
        f"{BASE}/{preview}", json={"user_id": str(admin.id)}, headers=bearer(admin.token)
    )

    assert response.status_code == 404
    assert response.json()["type"] == NO_USER


async def test_an_ended_preview_takes_no_switch_and_no_page_view(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    admin = await an_admin(db_engine)
    user = await a_user(db_engine)
    preview = (await start(client, admin, user))["id"]
    ended = await client.post(
        f"{BASE}/{preview}/end", json={"reason": "exit"}, headers=bearer(admin.token)
    )
    assert ended.status_code == 204

    switched = await client.patch(
        f"{BASE}/{preview}", json={"user_id": str(user)}, headers=bearer(admin.token)
    )
    viewed = await client.post(
        f"{BASE}/{preview}/page-views", json={"path": "/x"}, headers=bearer(admin.token)
    )

    assert switched.status_code == 409
    assert viewed.status_code == 409
    assert [e["kind"] for e in await events(db_engine, preview)] == ["start", "end"]


@pytest.mark.parametrize("first", ["switch", "end"])
async def test_a_lapsed_preview_takes_no_switch_and_keeps_its_lapse_on_end(
    client: httpx.AsyncClient, db_engine: AsyncEngine, first: str
) -> None:
    admin = await an_admin(db_engine)
    user = await a_user(db_engine)
    preview = (await start(client, admin, user))["id"]
    await age(db_engine, preview, started=40, active=31)

    if first == "switch":
        response = await client.patch(
            f"{BASE}/{preview}", json={"user_id": str(user)}, headers=bearer(admin.token)
        )
        assert response.status_code == 409
    else:
        response = await client.post(
            f"{BASE}/{preview}/end", json={"reason": "exit"}, headers=bearer(admin.token)
        )
        assert response.status_code == 204

    log = await events(db_engine, preview)
    assert [(e["kind"], e["end_reason"]) for e in log] == [("start", None), ("end", "idle")]


async def test_an_ended_preview_is_409_whoever_the_switch_asks_for(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    admin = await an_admin(db_engine)
    preview = (await start(client, admin, await a_user(db_engine)))["id"]
    await client.post(f"{BASE}/{preview}/end", json={"reason": "exit"}, headers=bearer(admin.token))

    response = await client.patch(
        f"{BASE}/{preview}", json={"user_id": str(admin.id)}, headers=bearer(admin.token)
    )

    assert response.status_code == 409


async def test_ending_is_idempotent_and_keeps_the_first_reason(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    admin = await an_admin(db_engine)
    preview = (await start(client, admin, await a_user(db_engine)))["id"]

    for reason in ("sign_out", "exit"):
        response = await client.post(
            f"{BASE}/{preview}/end", json={"reason": reason}, headers=bearer(admin.token)
        )
        assert response.status_code == 204

    body = (await client.get(f"{BASE}/{preview}", headers=bearer(admin.token))).json()
    assert body["end_reason"] == "sign_out"
    log = await events(db_engine, preview)
    assert [(e["kind"], e["end_reason"]) for e in log] == [("start", None), ("end", "sign_out")]


@pytest.mark.parametrize("reason", ["idle", "max_length", "nope"])
async def test_an_admin_ends_with_exit_or_sign_out_only(
    client: httpx.AsyncClient, db_engine: AsyncEngine, reason: str
) -> None:
    admin = await an_admin(db_engine)
    preview = (await start(client, admin, await a_user(db_engine)))["id"]

    response = await client.post(
        f"{BASE}/{preview}/end", json={"reason": reason}, headers=bearer(admin.token)
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("aged", "reason", "lapsed_after"),
    [
        ({"started": 40, "active": 31}, "idle", 39),
        ({"started": 121, "active": 2}, "max_length", 120),
    ],
    ids=["idle", "max_length"],
)
async def test_a_read_ends_a_lapsed_preview_once_at_the_moment_it_lapsed(
    client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    aged: dict[str, int],
    reason: str,
    lapsed_after: int,
) -> None:
    admin = await an_admin(db_engine)
    preview = (await start(client, admin, await a_user(db_engine)))["id"]
    await age(db_engine, preview, **aged)

    first = await client.get(f"{BASE}/{preview}", headers=bearer(admin.token))
    second = await client.get(f"{BASE}/{preview}", headers=bearer(admin.token))

    assert first.status_code == 200
    body = first.json()
    assert body["end_reason"] == reason
    assert second.json() == body
    assert datetime.fromisoformat(body["ended_at"]) - datetime.fromisoformat(
        body["started_at"]
    ) == timedelta(minutes=lapsed_after)
    log = await events(db_engine, preview)
    assert [(e["kind"], e["end_reason"]) for e in log] == [("start", None), ("end", reason)]


async def test_a_live_preview_inside_its_limits_stays_live(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    admin = await an_admin(db_engine)
    preview = (await start(client, admin, await a_user(db_engine)))["id"]
    await age(db_engine, preview, started=119, active=29)

    body = (await client.get(f"{BASE}/{preview}", headers=bearer(admin.token))).json()

    assert body["ended_at"] is None


async def test_a_lapsed_preview_takes_no_page_view(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    admin = await an_admin(db_engine)
    preview = (await start(client, admin, await a_user(db_engine)))["id"]
    await age(db_engine, preview, started=40, active=31)

    response = await client.post(
        f"{BASE}/{preview}/page-views", json={"path": "/x"}, headers=bearer(admin.token)
    )

    assert response.status_code == 409
    log = await events(db_engine, preview)
    assert [(e["kind"], e["end_reason"]) for e in log] == [("start", None), ("end", "idle")]


async def test_a_page_view_logs_the_path_only_and_counts_as_activity(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    admin = await an_admin(db_engine)
    user = await a_user(db_engine)
    preview = (await start(client, admin, user))["id"]
    await age(db_engine, preview, active=20)

    response = await client.post(
        f"{BASE}/{preview}/page-views",
        json={"path": "/mentors/ada?tab=reviews#top"},
        headers=bearer(admin.token),
    )

    assert response.status_code == 204
    log = await events(db_engine, preview)
    assert [(e["kind"], e["path"], e["target_user_id"]) for e in log] == [
        ("start", None, user),
        ("page_view", "/mentors/ada", user),
    ]
    async with db_engine.connect() as conn:
        fresh = await conn.scalar(
            text("SELECT last_active_at > now() - interval '1 minute' FROM preview_sessions")
        )
    assert fresh is True


@pytest.mark.parametrize("path", ["mentors", "", "/" + "p" * PAGE_PATH_MAX_LENGTH])
async def test_a_page_view_path_is_bounded(
    client: httpx.AsyncClient, db_engine: AsyncEngine, path: str
) -> None:
    admin = await an_admin(db_engine)
    preview = (await start(client, admin, await a_user(db_engine)))["id"]

    response = await client.post(
        f"{BASE}/{preview}/page-views", json={"path": path}, headers=bearer(admin.token)
    )

    assert response.status_code == 422


# --------------------------------------------------------------------------
# /me and the plan
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "revoked", "on", "expected"),
    [
        ("limited_access", False, True, True),
        ("super_admin", False, True, True),
        ("super_admin", False, False, False),
        ("super_admin", True, True, False),
        (None, False, True, False),
    ],
    ids=["live-grant", "super", "switched-off", "revoked", "no-grant"],
)
async def test_me_says_whether_the_caller_may_preview(
    db_engine: AsyncEngine, role: str | None, revoked: bool, on: bool, expected: bool
) -> None:
    auth_id = uuid4()
    await make_user(db_engine, auth_id, f"me-{auth_id}@example.test", role=role, revoked=revoked)

    async with app_client(db_engine, on=on) as c:
        response = await c.get("/api/v1/me", headers=bearer(api_token(auth_id)))

    assert response.status_code == 200, response.text
    assert response.json()["can_preview_as_user"] is expected


async def test_the_preview_lookup_is_by_primary_key(db_engine: AsyncEngine) -> None:
    """The guardrail's `EXPLAIN` on the store's own statement. `enable_seqscan =
    off` asks whether a usable index exists, not what the planner picks over a
    dozen rows."""
    owner = Owner(admin_id=uuid4(), auth_session_id=uuid4())
    statement = read_statement(owner, uuid4()).compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    )

    async with db_engine.connect() as conn:
        await conn.execute(text("SET enable_seqscan = off"))
        plan = "\n".join(
            str(line) for line in (await conn.execute(text(f"EXPLAIN {statement}"))).scalars()
        )

    assert "pk_preview_sessions" in plan, plan
    assert "Seq Scan" not in plan, plan
