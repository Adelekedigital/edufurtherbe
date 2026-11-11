"""Serving a preview as the previewed user, read only (decision 313).

With `X-EF-Preview` beside an admin's own token, every route that resolves a
caller answers as the previewed user; every write is refused `403` and logged.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.routing import APIRoute
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import make_public_mentor
from tests.integration.test_api_admin import make_user
from tests.integration.test_api_preview_sessions import (
    Admin,
    a_user,
    age,
    an_admin,
    app_client,
    events,
    start,
)

from app.api.deps.core import PREVIEW_HEADER, get_current_user
from app.core.config import Settings
from app.main import create_app
from conftest import PROBLEM_JSON, api_token, bearer, client_for

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

ME = "/api/v1/me"
ENDED = "/problems/preview-ended"
READ_ONLY = "/problems/read-only-view-as"
NOT_FOUND = "/problems/preview-not-found"

#: Writes that resolve no caller, so a preview can never reach them: each
#: authenticates as a machine (a signed callback or job), never as a user.
NO_CALLER = {
    ("POST", "/api/v1/callbacks/reminders"),
    ("POST", "/api/v1/callbacks/daily"),
    ("POST", "/api/v1/callbacks/bio-drafts"),
    ("POST", "/api/v1/callbacks/message-emails"),
    ("POST", "/api/v1/callbacks/push-messages"),
    ("POST", "/api/v1/internal/jobs/{job_name}"),
}


def previewing(admin: Admin, preview: str, token: str | None = None) -> dict[str, str]:
    return bearer(token or admin.token) | {PREVIEW_HEADER: preview}


async def live_preview(client: httpx.AsyncClient, engine: AsyncEngine) -> tuple[Admin, UUID, str]:
    admin = await an_admin(engine)
    user = await a_user(engine, role="mentee")
    return admin, user, (await start(client, admin, user))["id"]


async def preview_row(engine: AsyncEngine, preview: str) -> dict[str, Any]:
    async with engine.connect() as conn:
        row = await conn.execute(
            text(
                "SELECT ended_at, end_reason, last_active_at, now() AS now "
                "FROM preview_sessions WHERE id = :p"
            ),
            {"p": preview},
        )
        return dict(row.mappings().one())


async def blocked(engine: AsyncEngine, preview: str) -> list[tuple[str, str]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT method, endpoint FROM preview_events "
                "WHERE session_id = :s AND kind = 'blocked_write' ORDER BY created_at, id"
            ),
            {"s": preview},
        )
        return [(row.method, row.endpoint) for row in rows]


def assert_read_only(response: httpx.Response) -> None:
    assert response.status_code == 403, response.text
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    body = response.json()
    assert (body["type"], body["title"], body["code"]) == (
        READ_ONLY,
        "Read only",
        "read_only_view_as",
    )


def assert_preview_not_found(response: httpx.Response) -> None:
    """The one answer for every header that names no usable preview: the same
    type, title and detail whatever the reason, so none is told from another."""
    assert response.status_code == 404, response.text
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    body = response.json()
    assert (body["type"], body["title"], body["detail"]) == (
        NOT_FOUND,
        "Preview not found",
        "no such preview",
    )


def assert_ended(response: httpx.Response, preview: str, reason: str) -> None:
    assert response.status_code == 401, response.text
    body = response.json()
    assert (body["type"], body["end_reason"], body["session_id"]) == (ENDED, reason, preview)


# --------------------------------------------------------------------------
# The swap
# --------------------------------------------------------------------------


@pytest.mark.isolated_db
async def test_with_the_header_the_caller_is_the_previewed_user(db_engine: AsyncEngine) -> None:
    async with app_client(db_engine) as client:
        admin, user, preview = await live_preview(client, db_engine)

        as_target = await client.get(ME, headers=previewing(admin, preview))
        as_admin = await client.get(ME, headers=bearer(admin.token))

    assert as_target.status_code == 200, as_target.text
    assert as_target.json()["id"] == str(user)
    assert as_target.json()["is_admin"] is False
    assert as_target.json()["can_preview_as_user"] is False
    assert as_admin.json()["id"] == str(admin.id)
    assert as_admin.json()["is_admin"] is True


@pytest.mark.isolated_db
async def test_a_target_given_a_grant_since_is_still_served_without_it(
    db_engine: AsyncEngine,
) -> None:
    async with app_client(db_engine) as client:
        admin, user, preview = await live_preview(client, db_engine)
        async with db_engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO admin_users (user_id, admin_role) VALUES (:u, 'super_admin')"),
                {"u": user},
            )

        me = await client.get(ME, headers=previewing(admin, preview))
        admin_route = await client.get(
            "/api/v1/admin/mentors/summary", headers=previewing(admin, preview)
        )

    assert me.json()["is_admin"] is False
    assert me.json()["admin_role"] is None
    assert admin_route.status_code == 404


@pytest.mark.isolated_db
async def test_an_admin_route_with_the_header_is_404(db_engine: AsyncEngine) -> None:
    async with app_client(db_engine) as client:
        admin, _, preview = await live_preview(client, db_engine)

        queue = await client.get(
            "/api/v1/admin/mentors/summary", headers=previewing(admin, preview)
        )
        own = await client.get(
            f"/api/v1/admin/preview-sessions/{preview}", headers=previewing(admin, preview)
        )

    # The preview is valid, so these are the routes' own 404s, unchanged.
    for response in (queue, own):
        assert response.status_code == 404
        assert response.json()["type"] == "about:blank"


# --------------------------------------------------------------------------
# A header that names no preview of this admin's: 404
# --------------------------------------------------------------------------


async def test_a_header_with_previews_off_is_404(db_engine: AsyncEngine) -> None:
    async with app_client(db_engine) as client:
        admin, _, preview = await live_preview(client, db_engine)
    async with app_client(db_engine, on=False) as off:
        response = await off.get(ME, headers=previewing(admin, preview))

    assert_preview_not_found(response)


async def test_an_admin_whose_grant_is_revoked_loses_their_own_preview(
    db_engine: AsyncEngine,
) -> None:
    async with app_client(db_engine) as client:
        admin, _, preview = await live_preview(client, db_engine)
        async with db_engine.begin() as conn:
            await conn.execute(
                text("UPDATE admin_users SET revoked_at = now() WHERE user_id = :u"),
                {"u": admin.id},
            )

        response = await client.get(ME, headers=previewing(admin, preview))

    assert_preview_not_found(response)
    assert (await preview_row(db_engine, preview))["ended_at"] is None


@pytest.mark.parametrize("revoked", [False, True], ids=["no-grant", "revoked"])
async def test_a_header_with_no_preview_permission_behind_it_is_404(
    db_engine: AsyncEngine, revoked: bool
) -> None:
    async with app_client(db_engine) as client:
        _, _, preview = await live_preview(client, db_engine)
        auth_id = uuid4()
        await make_user(
            db_engine,
            auth_id,
            f"plain-{auth_id}@example.test",
            role="super_admin" if revoked else None,
            revoked=revoked,
        )

        response = await client.get(
            ME, headers=bearer(api_token(auth_id)) | {PREVIEW_HEADER: preview}
        )

    assert_preview_not_found(response)


@pytest.mark.isolated_db
@pytest.mark.parametrize("named", ["unknown", "malformed", "another-admins"])
async def test_a_header_naming_no_preview_of_this_admin_is_404(
    db_engine: AsyncEngine, named: str
) -> None:
    async with app_client(db_engine) as client:
        admin, _, mine = await live_preview(client, db_engine)
        _, _, theirs = await live_preview(client, db_engine)
        header = {"unknown": str(uuid4()), "malformed": "not-a-uuid", "another-admins": theirs}

        response = await client.get(ME, headers=previewing(admin, header[named]))

    assert_preview_not_found(response)
    assert (await preview_row(db_engine, theirs))["ended_at"] is None
    assert (await preview_row(db_engine, mine))["ended_at"] is None


@pytest.mark.isolated_db
async def test_a_404_that_is_not_about_the_header_is_unchanged(db_engine: AsyncEngine) -> None:
    async with app_client(db_engine) as client:
        admin, user, preview = await live_preview(client, db_engine)
        async with db_engine.begin() as conn:
            await conn.execute(
                text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": user}
            )

        gone = await client.get(ME, headers=previewing(admin, preview))
        unlinked = await client.get(ME, headers=bearer(api_token(uuid4(), email=None)))

    # A previewed user deleted since, and a token with no account: plain 404s.
    for response in (gone, unlinked):
        assert response.status_code == 404, response.text
        assert response.json()["type"] == "about:blank"
        assert response.json()["title"] == "NotFoundError"


# --------------------------------------------------------------------------
# An ended preview: 401 preview-ended
# --------------------------------------------------------------------------


@pytest.mark.isolated_db
async def test_a_preview_from_another_sign_in_ends_as_sign_out(db_engine: AsyncEngine) -> None:
    async with app_client(db_engine) as client:
        admin, _, preview = await live_preview(client, db_engine)

        first = await client.get(ME, headers=previewing(admin, preview, admin.another_sign_in()))
        again = await client.get(ME, headers=previewing(admin, preview))

    assert_ended(first, preview, "sign_out")
    assert_ended(again, preview, "sign_out")
    assert (await preview_row(db_engine, preview))["end_reason"] == "sign_out"
    ends = [e for e in await events(db_engine, preview) if e["kind"] == "end"]
    assert [e["end_reason"] for e in ends] == ["sign_out"]


@pytest.mark.isolated_db
@pytest.mark.parametrize(
    ("aged", "reason"),
    [({"active": 31}, "idle"), ({"started": 121}, "max_length")],
    ids=["idle", "max_length"],
)
async def test_a_lapsed_preview_ends_as_it_lapsed(
    db_engine: AsyncEngine, aged: dict[str, int], reason: str
) -> None:
    async with app_client(db_engine) as client:
        admin, _, preview = await live_preview(client, db_engine)
        await age(db_engine, preview, **aged)

        response = await client.get(ME, headers=previewing(admin, preview))

    assert_ended(response, preview, reason)
    assert (await preview_row(db_engine, preview))["end_reason"] == reason


@pytest.mark.isolated_db
async def test_a_lapsed_preview_from_another_sign_in_ends_as_it_lapsed(
    db_engine: AsyncEngine,
) -> None:
    async with app_client(db_engine) as client:
        admin, _, preview = await live_preview(client, db_engine)
        await age(db_engine, preview, active=31)

        response = await client.get(ME, headers=previewing(admin, preview, admin.another_sign_in()))

    assert_ended(response, preview, "idle")


@pytest.mark.isolated_db
async def test_an_exited_preview_answers_the_reason_it_ended_with(db_engine: AsyncEngine) -> None:
    async with app_client(db_engine) as client:
        admin, _, preview = await live_preview(client, db_engine)
        await client.post(
            f"/api/v1/admin/preview-sessions/{preview}/end",
            json={"reason": "exit"},
            headers=bearer(admin.token),
        )

        response = await client.get(ME, headers=previewing(admin, preview))

    assert_ended(response, preview, "exit")


# --------------------------------------------------------------------------
# Activity: at most once a minute
# --------------------------------------------------------------------------


@pytest.mark.isolated_db
async def test_a_request_moves_last_active_at_at_most_once_a_minute(
    db_engine: AsyncEngine,
) -> None:
    async with app_client(db_engine) as client:
        admin, _, preview = await live_preview(client, db_engine)
        await age(db_engine, preview, active=2)
        stale = (await preview_row(db_engine, preview))["last_active_at"]

        await client.get(ME, headers=previewing(admin, preview))
        touched = await preview_row(db_engine, preview)
        await client.get(ME, headers=previewing(admin, preview))
        untouched = await preview_row(db_engine, preview)

    assert touched["last_active_at"] > stale
    assert (touched["now"] - touched["last_active_at"]).total_seconds() < 5
    assert untouched["last_active_at"] == touched["last_active_at"]


@pytest.mark.isolated_db
async def test_a_request_within_the_minute_leaves_last_active_at(db_engine: AsyncEngine) -> None:
    async with app_client(db_engine) as client:
        admin, _, preview = await live_preview(client, db_engine)
        async with db_engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE preview_sessions SET last_active_at = now() - interval '50 seconds' "
                    "WHERE id = :p"
                ),
                {"p": preview},
            )
        before = (await preview_row(db_engine, preview))["last_active_at"]

        await client.get(ME, headers=previewing(admin, preview))

    assert (await preview_row(db_engine, preview))["last_active_at"] == before


# --------------------------------------------------------------------------
# Read only
# --------------------------------------------------------------------------


def _routes(routes: list[Any]) -> Iterator[APIRoute]:
    for route in routes:
        if hasattr(route, "original_router"):
            yield from _routes(route.original_router.routes)
        elif isinstance(route, APIRoute):
            yield route


def _resolves_a_caller(route: APIRoute) -> bool:
    pending = list(route.dependant.dependencies)
    while pending:
        dependency = pending.pop()
        if dependency.call is get_current_user:
            return True
        pending.extend(dependency.dependencies)
    return False


def _writes() -> list[tuple[str, str, bool]]:
    app = create_app(Settings(_env_file=None, preview_as_user_enabled=True))
    return [
        (method, route.path, _resolves_a_caller(route))
        for route in _routes(app.routes)
        for method in sorted(route.methods - {"GET", "HEAD", "OPTIONS"})
    ]


async def test_only_machine_routes_write_without_resolving_a_caller() -> None:
    """Pinned: a new write that resolves no caller is reviewed for previews."""
    writes = _writes()
    assert len(writes) > 50
    assert {(m, p) for m, p, caller in writes if not caller} == NO_CALLER


@pytest.mark.isolated_db
async def test_every_write_in_a_preview_is_refused_and_logged(db_engine: AsyncEngine) -> None:
    writes = [(method, path) for method, path, caller in _writes() if caller]
    async with app_client(db_engine) as client:
        admin, user, preview = await live_preview(client, db_engine)
        for method, path in writes:
            filled = re.sub(r"\{[^}]+\}", str(uuid4()), path)
            response = await client.request(
                method, filled, json={}, headers=previewing(admin, preview)
            )
            assert response.status_code == 403, (method, path, response.text)
            assert_read_only(response)

    assert await blocked(db_engine, preview) == writes
    assert (await preview_row(db_engine, preview))["ended_at"] is None
    async with db_engine.connect() as conn:
        sessions = await conn.scalar(
            text("SELECT count(*) FROM sessions WHERE mentee_id = :u OR mentor_id = :u"),
            {"u": user},
        )
    assert sessions == 0


@pytest.mark.isolated_db
async def test_a_write_in_an_ended_preview_is_401_and_logs_no_blocked_write(
    db_engine: AsyncEngine,
) -> None:
    async with app_client(db_engine) as client:
        admin, _, preview = await live_preview(client, db_engine)
        await age(db_engine, preview, active=31)

        response = await client.post(
            "/api/v1/sessions", json={}, headers=previewing(admin, preview)
        )

    assert_ended(response, preview, "idle")
    assert await blocked(db_engine, preview) == []


async def test_a_write_without_the_header_is_the_admins_own(db_engine: AsyncEngine) -> None:
    async with app_client(db_engine) as client:
        admin, _, preview = await live_preview(client, db_engine)

        response = await client.post("/api/v1/sessions", json={}, headers=bearer(admin.token))

    assert response.status_code == 422
    assert await blocked(db_engine, preview) == []


@pytest.mark.isolated_db
async def test_a_read_refused_in_a_preview_is_403_and_logged(db_engine: AsyncEngine) -> None:
    """`refuse_in_preview` on a GET: the calendar consent link, whose sealed
    state would attach a calendar to the previewed user's account."""
    path = "/api/v1/me/calendar/connect"
    async with app_client(db_engine) as client:
        admin, _, preview = await live_preview(client, db_engine)

        refused = await client.get(path, headers=previewing(admin, preview))
        own = await client.get(path, headers=bearer(admin.token))

    assert_read_only(refused)
    assert await blocked(db_engine, preview) == [("GET", path)]
    assert own.status_code != 403


@pytest.mark.isolated_db
async def test_reads_in_a_preview_log_nothing(db_engine: AsyncEngine) -> None:
    async with app_client(db_engine) as client:
        admin, _, preview = await live_preview(client, db_engine)

        response = await client.get("/api/v1/me/messages", headers=previewing(admin, preview))

    assert response.status_code == 200, response.text
    assert await blocked(db_engine, preview) == []


# --------------------------------------------------------------------------
# Routes anyone may read
# --------------------------------------------------------------------------


@pytest.mark.isolated_db
async def test_a_public_profile_is_shown_as_the_previewed_mentor_sees_it(
    db_engine: AsyncEngine,
) -> None:
    async with app_client(db_engine) as client:
        admin = await an_admin(db_engine)
        mentor = await make_public_mentor(db_engine, str(uuid4()), approved=False, listed=False)
        preview = (await start(client, admin, mentor))["id"]
        path = f"/api/v1/mentors/{mentor}"

        as_mentor = await client.get(path, headers=previewing(admin, preview))
        as_admin = await client.get(path, headers=bearer(admin.token))

    assert as_mentor.status_code == 200, as_mentor.text
    assert as_mentor.json()["approval_status"] == "pending"
    assert "x-ef-preview" in as_admin.headers["vary"].lower()
    assert "approval_status" not in as_admin.json()


@pytest.mark.isolated_db
@pytest.mark.parametrize("token", [True, False], ids=["with-token", "header-only"])
async def test_a_public_route_does_not_read_a_bad_preview_as_anonymous(
    db_engine: AsyncEngine, token: bool
) -> None:
    async with app_client(db_engine) as client:
        admin, _, preview = await live_preview(client, db_engine)
        await age(db_engine, preview, active=31)
        headers = previewing(admin, preview) if token else {PREVIEW_HEADER: preview}

        ended = await client.get("/api/v1/mentors", headers=headers)
        unknown = await client.get("/api/v1/mentors", headers=previewing(admin, str(uuid4())))
        anonymous = await client.get("/api/v1/mentors")

    if token:
        assert_ended(ended, preview, "idle")
    else:
        assert ended.status_code == 401
    assert_preview_not_found(unknown)
    assert anonymous.status_code == 200


# --------------------------------------------------------------------------
# The contract
# --------------------------------------------------------------------------


async def test_a_browser_may_send_the_preview_header() -> None:
    origin = "https://app.edufurther.org"
    app = create_app(Settings(_env_file=None, environment="ci", cors_origins=[origin]))
    async with client_for(app) as client:
        response = await client.options(
            ME,
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization,x-ef-preview",
            },
        )

    assert response.status_code == 200
    assert "x-ef-preview" in response.headers["access-control-allow-headers"].lower()


async def test_the_spec_documents_the_header_and_its_refusals_where_it_is_honoured(
    openapi_spec: dict[str, Any],
) -> None:
    paths = openapi_spec["paths"]

    def headers(operation: dict[str, Any]) -> set[str]:
        return {p["name"] for p in operation.get("parameters", []) if p["in"] == "header"}

    me, book, explore = (
        paths[ME]["get"],
        paths["/api/v1/sessions"]["post"],
        paths["/api/v1/mentors"]["get"],
    )
    for operation in (me, book, explore):
        assert PREVIEW_HEADER in headers(operation)
        assert ENDED in operation["responses"]["401"]["description"]
        assert NOT_FOUND in operation["responses"]["404"]["description"]
    assert READ_ONLY in book["responses"]["403"]["description"]
    assert "403" not in me["responses"]
    assert (
        READ_ONLY in paths["/api/v1/me/calendar/connect"]["get"]["responses"]["403"]["description"]
    )
    public = paths["/api/v1/featured-mentor"]["get"]
    assert PREVIEW_HEADER not in headers(public)
    assert "401" not in public["responses"]
