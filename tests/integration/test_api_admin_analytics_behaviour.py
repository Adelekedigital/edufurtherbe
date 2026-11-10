"""The admin Analytics page's PostHog reads (PRD 31 PR 5, decision 331): the gate,
the `503`, the database tabs answering without PostHog, the alert and the cache."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import admin_token, token_for
from tests.posthog_fakes import (
    CARD_COLUMNS,
    HOST,
    Handler,
    body_of,
    funnel_answer,
    posthog_reader,
    rows,
)

from app.core.config import Settings
from app.infra.storage.supabase import SupabaseStorage
from conftest import add_user, build_api_app, client_for

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

BASE = "/api/v1/admin/analytics"


class PostHog:
    """A fake PostHog: answers each query by its kind and name, and counts."""

    def __init__(self, *, login: tuple[int, int, int] = (100, 5, 0), down: bool = False) -> None:
        self.login = login
        self.down = down
        self.seen: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        query = body_of(request)["query"]
        self.seen.append(query)
        if self.down:
            return httpx.Response(502, text="bad gateway")
        if query["kind"] == "FunnelsQuery":
            return funnel_answer(*(50 - 10 * i for i in range(len(query["series"]))))
        name = query["name"]
        if name == "admin_analytics_login_codes":
            return rows(["attempts", "failed", "failed_migrated"], list(self.login))
        if name == "admin_analytics_cards":
            return rows(CARD_COLUMNS, [10, 2, 1, 8, 2, 3, 8, 5, 1, 3.0, 6, 3, 2, 1, 2, 20])
        if name in ("admin_analytics_profile_sources", "admin_analytics_failed_routes"):
            return rows(["key", "count"], ["explore", 3], ["home", 1])
        if name == "admin_analytics_explore":
            return rows(["g", "searched", "led"], [1, 10, 4], [2, 8, 2])
        if name == "admin_analytics_explore_booked":
            return rows(["booked"], [2])
        raise AssertionError(name)

    def asked(self, name: str) -> int:
        return sum(1 for q in self.seen if q.get("name", q["kind"]) == name)


def app_for(
    engine: AsyncEngine,
    storage: SupabaseStorage | None,
    posthog: Handler | None,
    *,
    on: bool = True,
    **settings: Any,
) -> Any:
    app = build_api_app(
        engine, storage, Settings(_env_file=None, admin_analytics_enabled=on, **settings)
    )
    if posthog is not None:
        app.state.analytics_reader = posthog_reader(posthog, project="7")[0]
    return app


async def get(client: httpx.AsyncClient, token: str, path: str, **query: str) -> httpx.Response:
    return await client.get(BASE + path, params=query, headers={"Authorization": f"Bearer {token}"})


async def test_switched_off_or_not_an_admin_is_a_404(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    admin = await admin_token(db_engine)
    async with db_engine.begin() as conn:
        stranger = await token_for(
            db_engine, await add_user(conn, "stranger@example.test", primary_role="mentee")
        )
    async with client_for(app_for(db_engine, api_storage, PostHog(), on=False)) as client:
        for path in ("/funnels/signup", "/behaviour"):
            assert (await get(client, admin, path)).status_code == 404
            assert (await client.get(BASE + path)).status_code == 404
    async with client_for(app_for(db_engine, api_storage, PostHog())) as client:
        for path in ("/funnels/signup", "/behaviour"):
            assert (await get(client, stranger, path)).status_code == 404
            for role in ("limited_access", "mentor_approval", "super_admin"):
                answer = await get(client, await admin_token(db_engine, role), path)
                assert answer.status_code == 200, (path, role)
                assert answer.headers["Cache-Control"] == "private, no-store"


async def test_a_funnel_returns_every_step_compared_and_refuses_what_it_has_not(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    token = await admin_token(db_engine)
    posthog = PostHog()
    async with client_for(app_for(db_engine, api_storage, posthog)) as client:
        answer = await get(client, token, "/funnels/signup", by="migrated", window="7d")
    assert answer.status_code == 200
    body = answer.json()
    assert (body["name"], body["by"], body["window_days"]) == ("signup", "migrated", 30)
    assert body["note"].startswith("One sign-up funnel for both roles")
    keys = "started code_verified onboarded first_booking_or_bookable first_completed"
    assert [s["key"] for s in body["steps"]] == keys.split()
    first = {"key": "started", "label": "Started sign-up", "value": 50, "previous": 50}
    assert body["steps"][0] == first
    assert [s["value"] for s in body["steps"]] == [50, 40, 30, 20, 10]
    assert all(a["key"] != "login_code_failed" for a in body["alerts"])
    # This period and the previous: two funnel reads.
    assert posthog.asked("FunnelsQuery") == 2
    async with client_for(app_for(db_engine, api_storage, posthog)) as client:
        untracked = await get(client, token, "/funnels/mentor_activation", compare="false")
        assert (await get(client, token, "/funnels/sign_up")).status_code == 404
        refused = await get(client, token, "/funnels/booking", by="migrated")
    steps = untracked.json()["steps"]
    assert [s["value"] for s in steps] == [50, 40, 30, 20, None]
    assert all(s["previous"] is None for s in steps)
    assert posthog.asked("FunnelsQuery") == 3  # once without compare; none refused
    assert refused.status_code == 422
    assert refused.json()["type"] == "/problems/analytics-window"
    assert refused.json()["errors"][0]["pointer"] == "/query/by"


async def test_behaviour_returns_the_seven_cards_and_the_replay_link(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    token = await admin_token(db_engine)
    configured = {"analytics_api_host": HOST, "analytics_project_id": "7"}
    configured |= {"analytics_api_key": SecretStr("phx_test")}
    async with client_for(app_for(db_engine, api_storage, PostHog(), **configured)) as client:
        answer = await get(client, token, "/behaviour")
    assert answer.status_code == 200
    body = answer.json()
    login = {"attempts": 10, "failed_rate": 20.0, "failed_migrated_rate": 50.0}
    assert body["login_codes"] == login
    search = {"searches": 8, "no_results_rate": 25.0, "profile_views_from_search": 3}
    assert body["search"] == search
    shares = [{"source": "explore", "share": 37.5}, {"source": "home", "share": 12.5}]
    assert body["profile_views_by_source"] == shares
    joining = {"join_clicked": 5, "join_failed": 1, "median_minutes_from_start": 3.0}
    assert body["joining"] == joining
    assert body["writing_assist"] == {"used": 6, "accepted_rate": 50.0, "discarded_rate": 33.3}
    assert body["app"] == {"installed": 1, "push_allowed_rate": 10.0}
    routes = [{"route": "explore", "count": 3}, {"route": "home", "count": 1}]
    assert body["errors_by_screen"] == routes
    assert body["replay_url"] == f"{HOST}/project/7/replay/home"


@pytest.mark.parametrize("path", ["/funnels/signup", "/behaviour"])
async def test_posthog_down_or_unconfigured_is_a_503_with_retry_after(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None, path: str
) -> None:
    token = await admin_token(db_engine)
    for posthog in (None, PostHog(down=True)):
        async with client_for(app_for(db_engine, api_storage, posthog)) as client:
            answer = await get(client, token, path)
        assert answer.status_code == 503, posthog
        assert answer.headers["Retry-After"] == "60"
        assert answer.json()["type"] == "/problems/analytics-upstream"
        assert "posthog" not in answer.text.lower()


async def test_overview_fills_explore_and_answers_without_posthog(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    token = await admin_token(db_engine)
    async with client_for(app_for(db_engine, api_storage, PostHog())) as client:
        up = await get(client, token, "/overview")
    assert up.status_code == 200
    explore = next(f for f in up.json()["features"] if f["key"] == "explore")
    assert explore["value"] == {"value": 40.0, "previous": 25.0}
    assert explore["contribution"] == 2
    assert len(explore["spark"]) == 8

    for posthog in (None, PostHog(down=True)):
        async with client_for(app_for(db_engine, api_storage, posthog)) as client:
            down = await get(client, token, "/overview")
        assert down.status_code == 200
        features = {f["key"]: f for f in down.json()["features"]}
        empty = {"key": "explore", "value": None, "spark": None, "contribution": None}
        assert features["explore"] == empty
        assert features["booking"]["value"] is not None


async def test_the_login_alert_rides_every_section_over_ten_percent(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    token = await admin_token(db_engine)
    over = PostHog(login=(100, 11, 6))
    async with client_for(app_for(db_engine, api_storage, over)) as client:
        for path in ("/credits", "/funnels/signup"):
            alerts = (await get(client, token, path)).json()["alerts"]
            login = [a for a in alerts if a["key"] == "login_code_failed"]
            assert len(login) == 1, path
            assert login[0]["body"] == (
                "11% of log-in attempts failed yesterday (alert at 10%). Most are migrated users."
            )
            assert login[0]["link"] == {"tab": "funnels", "funnel": "signup", "by": "migrated"}
        # Yesterday's counts are read once, then kept.
        assert over.asked("admin_analytics_login_codes") == 1

    at_threshold = PostHog(login=(100, 10, 10))
    async with client_for(app_for(db_engine, api_storage, at_threshold)) as client:
        alerts = (await get(client, token, "/credits")).json()["alerts"]
    assert all(a["key"] != "login_code_failed" for a in alerts)

    async with client_for(app_for(db_engine, api_storage, PostHog(down=True))) as client:
        answer = await get(client, token, "/credits")
    assert answer.status_code == 200
    assert all(a["key"] != "login_code_failed" for a in answer.json()["alerts"])


async def test_internal_accounts_are_left_out_by_their_database_ids(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    token = await admin_token(db_engine)
    async with db_engine.begin() as conn:
        counted = await add_user(conn, "counted@example.test", primary_role="mentee")
        internal = (await conn.execute(text("SELECT id FROM users WHERE is_internal"))).scalars()
        internal_ids = {str(user) for user in internal}
    assert internal_ids, "an admin is an internal account"
    posthog = PostHog()
    async with client_for(app_for(db_engine, api_storage, posthog)) as client:
        assert (await get(client, token, "/funnels/signup", compare="false")).status_code == 200
        assert (await get(client, token, "/behaviour")).status_code == 200
    for query in posthog.seen:
        if query["kind"] == "HogQLQuery":
            sent = set(query["values"]["internal"])
        else:
            (left_out,) = (f["key"] for f in query["properties"] if "distinct_id" in f["key"])
            sent = {i for i in internal_ids | {str(counted)} if f"'{i}'" in left_out}
        assert sent == internal_ids, query.get("name", query["kind"])
        assert str(counted) not in sent


async def test_a_failure_is_kept_a_minute_on_optional_paths_and_never_on_posthog_routes(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    token = await admin_token(db_engine)
    posthog = PostHog(down=True)
    async with client_for(app_for(db_engine, api_storage, posthog)) as client:
        for _ in range(2):
            assert (await get(client, token, "/credits")).status_code == 200
        assert posthog.asked("admin_analytics_login_codes") == 1
        for _ in range(2):
            assert (await get(client, token, "/overview")).status_code == 200
        assert posthog.asked("admin_analytics_explore") == 1
        # The PostHog routes are never answered from a remembered failure...
        for _ in range(2):
            assert (await get(client, token, "/behaviour")).status_code == 503
        assert posthog.asked("admin_analytics_cards") == 2
        # ...and keep a result once there is one.
        posthog.down = False
        for _ in range(2):
            assert (await get(client, token, "/behaviour")).status_code == 200
        assert posthog.asked("admin_analytics_cards") == 3
