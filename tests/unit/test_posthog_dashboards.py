"""PostHog dashboards as code (decision 341): the definitions match admin
Analytics, and the sync rewrites the repo's items in place over a fake PostHog."""

from __future__ import annotations

import datetime as dt
import importlib.util
import itertools
import json
import logging
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

import httpx
import pytest
from tests.posthog_fakes import HOST, KEY

from app.core.errors import UpstreamError
from app.domain.analytics_behaviour import EVERYONE, FUNNELS
from app.domain.mentor_stats import Window
from app.infra.clients.posthog_admin import PostHogAdmin
from app.infra.clients.posthog_reader import funnel_query, not_internal
from app.infra.posthog_dashboards.definitions import (
    ACTIVE_USERS,
    DASHBOARDS,
    MANAGED_TAG,
    MENTOR_ACTIVATION_EVENTS,
    WEEKLY_RETENTION,
    insight_query,
)
from app.infra.posthog_dashboards.sync import described, sync

PREFIX = "/api/projects/42"
INSIGHTS = sum(len(d.insights) for d in DASHBOARDS)


class FakePostHog:
    """In-memory insights and dashboards behind a MockTransport, listed a
    few rows a page with a `next` link, as PostHog pages."""

    def __init__(self, page: int = 4) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {"insights": [], "dashboards": []}
        self.writes: list[tuple[str, str, Any]] = []
        self.ids = itertools.count(1)
        self.page = page
        self.keep_tags = True
        #: PostHog's `next` names its own host, which need not be ours.
        self.next_host = "https://app.posthog.example.test"

    def add(self, kind: str, **row: Any) -> dict[str, Any]:
        row = {"id": next(self.ids), "deleted": False, **row}
        self.rows[kind].append(row)
        return row

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        url = urlparse(str(request.url))
        parts = url.path.removeprefix(PREFIX).strip("/").split("/")
        body = json.loads(request.content) if request.content else None
        kind = parts[0]
        if request.method == "GET":
            offset = int(parse_qs(url.query).get("offset", ["0"])[0])
            rows = self.rows[kind][offset : offset + self.page]
            more = offset + self.page < len(self.rows[kind])
            following = f"{self.next_host}{url.path}?limit={self.page}&offset={offset + self.page}"
            return httpx.Response(200, json={"results": rows, "next": following if more else None})
        self.writes.append((request.method, url.path, body))
        if len(parts) == 1:
            created = self.add(kind, **body)
            if not self.keep_tags:
                created["tags"] = []
            return httpx.Response(201, json=created)
        row = next(r for r in self.rows[kind] if r["id"] == int(parts[1]))
        row.update(body)
        return httpx.Response(200, json=row)

    def admin(self) -> PostHogAdmin:
        client = httpx.Client(transport=httpx.MockTransport(self))
        return PostHogAdmin(api_host=HOST + "/", project_id="42", api_key=KEY, client=client)


def _all_insights() -> list[Any]:
    return [insight for dashboard in DASHBOARDS for insight in dashboard.insights]


# --- The definitions -----------------------------------------------------------


def test_every_funnel_insight_is_the_admin_query_but_for_its_window() -> None:
    window = Window(
        since=dt.datetime(2026, 11, 1, tzinfo=dt.UTC), until=dt.datetime(2026, 11, 8, tzinfo=dt.UTC)
    )
    internal = [uuid4(), uuid4()]
    funnels = [i for i in _all_insights() if i.funnel is not None]
    assert {i.funnel for i in funnels} == {
        "signup",
        "booking",
        "mentee_activation",
        "mentor_application",
    }
    for insight in funnels:
        ours = dict(insight_query(insight, internal)["source"])
        theirs = funnel_query(FUNNELS[insight.funnel], EVERYONE, window, internal)
        # A relative range for the asked window, and a breakdown funnel adds
        # PostHog's own breakdown. Everything else, filterTestAccounts and the
        # exclusion included, is the same.
        assert ours.pop("dateRange") == {"date_from": "-30d"}
        theirs.pop("dateRange")
        ours.pop("breakdownFilter", None)
        assert ours == theirs, insight.name


def test_mentor_activation_from_events_keeps_the_database_funnels_steps() -> None:
    steps = MENTOR_ACTIVATION_EVENTS.steps
    assert [s.events for s in steps] == [
        ("mentor_listed",),
        ("availability_set",),
        ("session_type_created",),
        ("booking_received",),
        ("session_delivered",),
    ]
    assert [s.label for s in steps] == [s.label for s in FUNNELS["mentor_activation"].steps]
    assert MENTOR_ACTIVATION_EVENTS.window_days == 30
    assert not MENTOR_ACTIVATION_EVENTS.in_database


def test_each_dashboard_lists_its_insights() -> None:
    assert {d.name: [i.name for i in d.insights] for d in DASHBOARDS} == {
        "Acquisition and sign-up": [
            "Sign-up funnel by source",
            "Sign-up funnel by channel",
            "Invite links shared",
        ],
        "Mentee journey": ["Mentee activation", "Booking funnel by entry point"],
        "Mentor journey": [
            "Mentor application",
            "Mentor activation (events; admin Analytics is exact)",
            "Calendars connected",
            "Mentor profile links shared",
        ],
        "Engagement": [
            "Active users (daily, weekly, monthly)",
            "Weekly retention",
            "Session join clicks by role",
            "Session join failures by role",
            "Messages sent by role",
            "Writing assist uses by role",
        ],
        "Reliability": [
            "Failed requests by route",
            "Server errors",
            "Push permission granted",
            "App installs",
        ],
    }


def test_every_key_is_unique() -> None:
    keys = [i.key for i in _all_insights()]
    assert len(set(keys)) == len(keys) == INSIGHTS == 19
    assert len({d.key for d in DASHBOARDS}) == len(DASHBOARDS) == 5


def test_every_insight_leaves_internal_accounts_out_as_admin_analytics_does() -> None:
    internal = [uuid4()]
    for insight in _all_insights():
        query = insight_query(insight, internal)
        assert query["kind"] == "InsightVizNode"
        source = query["source"]
        assert source["filterTestAccounts"] is False, insight.name
        assert source["properties"][0] == not_internal(internal), insight.name
        assert str(internal[0]) in source["properties"][0]["key"]
        assert source["dateRange"] == (
            {"date_from": "-8w"} if insight is WEEKLY_RETENTION else {"date_from": "-30d"}
        )


def test_active_users_counts_daily_weekly_and_monthly_by_role() -> None:
    source = insight_query(ACTIVE_USERS, [])["source"]
    assert [(s["event"], s["math"]) for s in source["series"]] == [
        (None, "dau"),
        (None, "weekly_active"),
        (None, "monthly_active"),
    ]
    assert source["breakdownFilter"] == {"breakdown": "role", "breakdown_type": "person"}
    assert source["interval"] == "day"
    assert ACTIVE_USERS.key == "active_users"


def test_weekly_retention_returns_on_any_event_over_eight_weeks_by_role() -> None:
    internal = [uuid4()]
    source = insight_query(WEEKLY_RETENTION, internal)["source"]
    assert source["kind"] == "RetentionQuery"
    retention = source["retentionFilter"]
    assert (retention["period"], retention["totalIntervals"]) == ("Week", 8)
    assert retention["targetEntity"] == retention["returningEntity"]
    assert retention["targetEntity"]["id"] is None
    assert retention["targetEntity"]["kind"] == "EventsNode"
    assert source["breakdownFilter"] == {"breakdown": "role", "breakdown_type": "person"}
    assert source["properties"] == [not_internal(internal)]
    assert source["filterTestAccounts"] is False
    assert WEEKLY_RETENTION.key == "weekly_retention"


def test_breakdowns_and_trend_filters_are_the_asked_ones() -> None:
    by_name = {i.name: i.source for i in _all_insights()}
    assert by_name["Sign-up funnel by channel"]["breakdownFilter"] == {
        "breakdown": "signup_channel",
        "breakdown_type": "person",
    }
    assert by_name["Booking funnel by entry point"]["breakdownFilter"]["breakdown_type"] == "event"
    assert by_name["Messages sent by role"]["breakdownFilter"]["breakdown"] == "role"
    assert by_name["Messages sent by role"]["interval"] == "day"
    shared = by_name["Mentor profile links shared"]["series"][0]
    assert [(p["key"], p["value"]) for p in shared["properties"]] == [
        ("kind", ["mentor_profile"]),
        ("own", ["true"]),
    ]
    assert by_name["Server errors"]["series"][0]["event"] == "$exception"


# --- The sync -------------------------------------------------------------------


@pytest.fixture
def internal() -> list[UUID]:
    return [uuid4(), uuid4()]


@pytest.fixture
def posthog() -> FakePostHog:
    return FakePostHog()


def test_first_apply_creates_everything_and_a_second_rewrites_in_place(
    posthog: FakePostHog, internal: list[UUID]
) -> None:
    first = sync(posthog.admin(), internal, apply=True)
    assert {c.action for c in first.changes} == {"create"}
    assert len(posthog.rows["dashboards"]) == 5
    assert len(posthog.rows["insights"]) == INSIGHTS
    before = len(posthog.writes)

    second = sync(posthog.admin(), internal, apply=True)

    assert {c.action for c in second.changes} == {"update"}
    assert len(second.changes) == len(first.changes) == 5 + INSIGHTS
    assert len(posthog.rows["dashboards"]) == 5
    assert len(posthog.rows["insights"]) == INSIGHTS
    rewritten = {path for method, path, _ in posthog.writes[before:] if method == "PATCH"}
    assert len(rewritten) == len(posthog.writes[before:]) == 5 + INSIGHTS


def test_insights_land_on_their_dashboard_with_the_internal_ids(
    posthog: FakePostHog, internal: list[UUID]
) -> None:
    sync(posthog.admin(), internal, apply=True)
    dashboards = {d["name"]: d["id"] for d in posthog.rows["dashboards"]}
    insight = next(i for i in posthog.rows["insights"] if i["name"] == "Server errors")
    assert insight["dashboards"] == [dashboards["Reliability"]]
    assert insight["tags"] == [MANAGED_TAG]
    exclusion = insight["query"]["source"]["properties"][0]["key"]
    assert all(str(user) in exclusion for user in internal)


def test_a_new_internal_account_is_left_out_on_the_next_run(
    posthog: FakePostHog, internal: list[UUID]
) -> None:
    sync(posthog.admin(), internal, apply=True)
    newcomer = uuid4()
    sync(posthog.admin(), [*internal, newcomer], apply=True)
    for insight in posthog.rows["insights"]:
        assert str(newcomer) in insight["query"]["source"]["properties"][0]["key"]


def test_hand_edits_are_overwritten_from_the_repo(
    posthog: FakePostHog, internal: list[UUID]
) -> None:
    sync(posthog.admin(), internal, apply=True)
    row = next(i for i in posthog.rows["insights"] if i["name"] == "App installs")
    original = row["query"]
    row["query"] = {"kind": "InsightVizNode", "source": {"kind": "TrendsQuery"}}

    sync(posthog.admin(), internal, apply=True)

    assert row["query"] == original


def test_a_renamed_definition_updates_in_place(posthog: FakePostHog, internal: list[UUID]) -> None:
    sync(posthog.admin(), internal, apply=True)
    row = next(i for i in posthog.rows["insights"] if i["name"] == "App installs")
    dash = next(d for d in posthog.rows["dashboards"] if d["name"] == "Reliability")
    # As if the repo had named them differently before: the key still matches.
    row["name"], dash["name"] = "Installs (old name)", "Health (old name)"

    plan = sync(posthog.admin(), internal, apply=True)

    assert (row["name"], dash["name"]) == ("App installs", "Reliability")
    assert "create" not in {c.action for c in plan.changes}
    assert len(posthog.rows["insights"]) == INSIGHTS


def test_a_dry_run_writes_nothing(posthog: FakePostHog, internal: list[UUID]) -> None:
    plan = sync(posthog.admin(), internal, apply=False)
    assert posthog.writes == []
    assert {c.action for c in plan.changes} == {"create"}
    assert plan.internal == 2

    sync(posthog.admin(), internal, apply=True)
    writes = len(posthog.writes)
    again = sync(posthog.admin(), internal, apply=False)
    assert len(posthog.writes) == writes
    assert {c.action for c in again.changes} == {"update"}


def test_only_tagged_keyed_items_are_matched_and_a_dropped_insight_leaves_the_dashboard(
    posthog: FakePostHog, internal: list[UUID]
) -> None:
    # Made by hand first, with our names and even our key line, but untagged.
    someone_elses = posthog.add(
        "dashboards", name="Reliability", tags=[], description=described("x", "reliability")
    )
    lookalike = posthog.add(
        "insights",
        name="App installs",
        tags=[],
        description=described("x", "app_installs"),
        dashboards=[someone_elses["id"]],
    )
    sync(posthog.admin(), internal, apply=True)
    assert len(posthog.rows["dashboards"]) == 6
    ours = posthog.rows["dashboards"][1]["id"]
    retired = posthog.add(
        "insights",
        name="Old",
        tags=[MANAGED_TAG],
        description=described("x", "retired"),
        dashboards=[ours, 999],
    )

    plan = sync(posthog.admin(), internal, apply=True)

    assert retired["dashboards"] == [999]
    assert [c.name for c in plan.changes if c.action == "remove"] == ["Old"]
    assert lookalike["dashboards"] == [someone_elses["id"]]
    assert someone_elses["description"] == described("x", "reliability")
    assert len(posthog.rows["insights"]) == INSIGHTS + 2


def test_an_insight_moved_by_hand_goes_back_to_its_one_managed_dashboard(
    posthog: FakePostHog, internal: list[UUID]
) -> None:
    sync(posthog.admin(), internal, apply=True)
    dashboards = {d["name"]: d["id"] for d in posthog.rows["dashboards"]}
    row = next(i for i in posthog.rows["insights"] if i["name"] == "App installs")
    row["dashboards"] = [dashboards["Engagement"], dashboards["Reliability"], 999]

    sync(posthog.admin(), internal, apply=True)

    assert sorted(row["dashboards"]) == sorted([dashboards["Reliability"], 999])


def test_an_item_that_lost_its_tag_is_deleted_and_the_run_stops(
    posthog: FakePostHog, internal: list[UUID]
) -> None:
    posthog.keep_tags = False
    with pytest.raises(UpstreamError, match="did not keep"):
        sync(posthog.admin(), internal, apply=True)
    (made,) = posthog.rows["dashboards"]
    assert made["deleted"] is True
    assert posthog.writes[-1] == ("PATCH", f"{PREFIX}/dashboards/{made['id']}/", {"deleted": True})


def test_lists_follow_next_across_pages(posthog: FakePostHog) -> None:
    for i in range(9):
        posthog.add("insights", name=f"i{i}", tags=[])
    assert [r["name"] for r in posthog.admin().insights()] == [f"i{i}" for i in range(9)]


def test_a_next_page_for_another_list_is_refused(posthog: FakePostHog) -> None:
    for i in range(5):
        posthog.add("insights", name=f"i{i}", tags=[])

    def elsewhere(request: httpx.Request) -> httpx.Response:
        body = json.loads(posthog(request).content)
        body["next"] = f"{HOST}/api/projects/43/insights/?offset=4"
        return httpx.Response(200, json=body)

    client = httpx.Client(transport=httpx.MockTransport(elsewhere))
    admin = PostHogAdmin(api_host=HOST, project_id="42", api_key=KEY, client=client)
    with pytest.raises(UpstreamError, match="next page elsewhere"):
        admin.insights()


# --- The script -------------------------------------------------------------


def _script() -> ModuleType:
    path = Path(__file__).parents[2] / "scripts" / "sync_posthog_dashboards.py"
    spec = importlib.util.spec_from_file_location("sync_posthog_dashboards", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _reading(monkeypatch: pytest.MonkeyPatch, script: ModuleType, internal: list[UUID]) -> None:
    async def read() -> list[UUID]:
        return internal

    monkeypatch.setattr(script, "read_internal", read)


def test_the_script_prints_the_plan(
    posthog: FakePostHog,
    internal: list[UUID],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = _script()
    _reading(monkeypatch, script, internal)
    assert script.run(False, posthog.admin()) == 0
    out = capsys.readouterr().out.splitlines()
    assert "dashboard create: Reliability" in out
    assert "insight create: Server errors" in out
    assert out[-2:] == [
        "internal accounts left out of every insight: 2",
        "dry run: nothing written",
    ]


def test_a_failure_says_what_was_written_and_never_the_key(
    internal: list[UUID],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    script = _script()
    posthog = FakePostHog()

    def refuse_insights(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/insights/"):
            return httpx.Response(403, text=f"key {KEY} lacks insight:write")
        return posthog(request)

    client = httpx.Client(transport=httpx.MockTransport(refuse_insights))
    admin = PostHogAdmin(api_host=HOST, project_id="42", api_key=KEY, client=client)
    _reading(monkeypatch, script, internal)
    caplog.set_level(logging.DEBUG)

    assert script.run(True, admin) == 1

    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.startswith(("written", "failed"))]
    assert len(lines) == 2
    assert lines[0] == "written before the failure: dashboards 5"
    assert lines[1].startswith("failed: UpstreamError: PostHog POST /insights/ failed")
    assert "lacks insight:write" in lines[1]
    assert KEY not in out
    assert KEY not in caplog.text


def test_a_malformed_answer_is_one_line_and_a_non_zero_exit(
    internal: list[UUID],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = _script()

    def malformed(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    client = httpx.Client(transport=httpx.MockTransport(malformed))
    admin = PostHogAdmin(api_host=HOST, project_id="42", api_key=KEY, client=client)
    _reading(monkeypatch, script, internal)

    assert script.run(False, admin) == 1

    out = capsys.readouterr().out
    assert [line for line in out.splitlines() if line.startswith("failed:")] == [
        "failed: KeyError: 'results'"
    ]
    assert "written before the failure: nothing" in out
