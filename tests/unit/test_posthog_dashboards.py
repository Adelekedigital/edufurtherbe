"""PostHog dashboards as code (decision 341): the definitions match admin
Analytics, and the sync is idempotent over a fake PostHog."""

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
from app.infra.clients.posthog_reader import funnel_query
from app.infra.posthog_dashboards.definitions import (
    COHORT_NAME,
    DASHBOARDS,
    MANAGED_TAG,
    MENTOR_ACTIVATION_EVENTS,
    excluded,
    insight_query,
)
from app.infra.posthog_dashboards.sync import sync

PREFIX = "/api/projects/42"


class FakePostHog:
    """In-memory cohorts, persons, insights and dashboards behind a MockTransport."""

    def __init__(self, persons: dict[str, str] | None = None) -> None:
        self.persons = persons or {}  # distinct id -> person uuid
        self.rows: dict[str, list[dict[str, Any]]] = {
            "insights": [],
            "dashboards": [],
            "cohorts": [],
        }
        self.members: dict[int, set[str]] = {}
        self.writes: list[tuple[str, str, Any]] = []
        self.ids = itertools.count(1)
        self.keep_tags = True

    def add(self, kind: str, **row: Any) -> dict[str, Any]:
        row = {"id": next(self.ids), "deleted": False, **row}
        self.rows[kind].append(row)
        return row

    def _page(self, rows: list[Any], query: dict[str, list[str]]) -> httpx.Response:
        offset, limit = int(query["offset"][0]), int(query["limit"][0])
        return httpx.Response(200, json={"results": rows[offset : offset + limit]})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        url = urlparse(str(request.url))
        query = parse_qs(url.query)
        parts = url.path.removeprefix(PREFIX).strip("/").split("/")
        body = json.loads(request.content) if request.content else None
        if request.method != "GET":
            self.writes.append((request.method, url.path, body))
        kind = parts[0]
        if kind == "persons":
            found = self.persons.get(query["distinct_id"][0])
            return httpx.Response(200, json={"results": [{"uuid": found}] if found else []})
        if len(parts) == 1:
            if request.method == "GET":
                return self._page(self.rows[kind], query)
            created = self.add(kind, **body)
            if not self.keep_tags:
                created["tags"] = []
            return httpx.Response(201, json=created)
        row_id = int(parts[1])
        if kind == "cohorts":
            members = self.members.setdefault(row_id, set())
            if parts[2] == "persons":
                by_uuid = {u: [d for d, p in self.persons.items() if p == u] for u in members}
                rows = [{"uuid": u, "distinct_ids": ids} for u, ids in sorted(by_uuid.items())]
                return self._page(rows, query)
            if parts[2] == "add_persons_to_static_cohort":
                members.update(body["person_ids"])
            else:
                members.discard(body["person_id"])
            return httpx.Response(200)
        row = next(r for r in self.rows[kind] if r["id"] == row_id)
        row.update(body)
        return httpx.Response(200, json=row)

    def admin(self) -> PostHogAdmin:
        client = httpx.Client(transport=httpx.MockTransport(self))
        return PostHogAdmin(api_host=HOST + "/", project_id="42", api_key=KEY, client=client)


def _all_insights() -> list[Any]:
    return [insight for dashboard in DASHBOARDS for insight in dashboard.insights]


def _without(query: dict[str, Any], *keys: str, drop: Any) -> dict[str, Any]:
    return {
        **{k: v for k, v in query.items() if k not in keys},
        "properties": [p for p in query["properties"] if not drop(p)],
    }


# --- The definitions -----------------------------------------------------------


def test_every_funnel_insight_is_the_admin_query_without_window_and_ids() -> None:
    window = Window(
        since=dt.datetime(2026, 11, 1, tzinfo=dt.UTC), until=dt.datetime(2026, 11, 8, tzinfo=dt.UTC)
    )
    funnels = [i for i in _all_insights() if i.funnel is not None]
    assert {i.funnel for i in funnels} == {
        "signup",
        "booking",
        "mentee_activation",
        "mentor_application",
    }
    for insight in funnels:
        source = insight_query(insight, 7)["source"]
        admin = funnel_query(FUNNELS[insight.funnel], EVERYONE, window, [uuid4()])
        ours = _without(
            source,
            "dateRange",
            "filterTestAccounts",
            "breakdownFilter",
            drop=lambda p: p == excluded(7),
        )
        theirs = _without(
            admin, "dateRange", "filterTestAccounts", drop=lambda p: p["type"] == "hogql"
        )
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


def test_every_insight_filters_test_accounts_and_leaves_the_cohort_out() -> None:
    for insight in _all_insights():
        query = insight_query(insight, 9)
        assert query["kind"] == "InsightVizNode"
        assert query["source"]["filterTestAccounts"] is True, insight.name
        assert excluded(9) in query["source"]["properties"], insight.name
        assert query["source"]["dateRange"] == {"date_from": "-30d"}


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
    return [uuid4(), uuid4(), uuid4()]


@pytest.fixture
def posthog(internal: list[UUID]) -> FakePostHog:
    # The third internal account has never sent an event.
    return FakePostHog({str(internal[0]): "p-0", str(internal[1]): "p-1", "outsider": "p-x"})


def test_first_apply_creates_everything_and_a_second_run_changes_nothing(
    posthog: FakePostHog, internal: list[UUID]
) -> None:
    first = sync(posthog.admin(), internal, apply=True)
    assert {c.action for c in first.changes} == {"create"}
    assert len(posthog.rows["dashboards"]) == 5
    assert len(posthog.rows["insights"]) == 17
    writes = len(posthog.writes)

    second = sync(posthog.admin(), internal, apply=True)

    assert {c.action for c in second.changes} == {"unchanged"}
    assert len(second.changes) == len(first.changes) == 1 + 5 + 17
    assert len(posthog.writes) == writes
    assert len(posthog.rows["insights"]) == 17


def test_insights_land_on_their_dashboard_excluding_the_synced_cohort(
    posthog: FakePostHog, internal: list[UUID]
) -> None:
    sync(posthog.admin(), internal, apply=True)
    cohort = posthog.rows["cohorts"][0]
    dashboards = {d["name"]: d["id"] for d in posthog.rows["dashboards"]}
    insight = next(i for i in posthog.rows["insights"] if i["name"] == "Server errors")
    assert insight["dashboards"] == [dashboards["Reliability"]]
    assert insight["tags"] == [MANAGED_TAG]
    assert excluded(cohort["id"]) in insight["query"]["source"]["properties"]


def test_the_cohort_holds_the_internal_accounts_and_nobody_else(
    posthog: FakePostHog, internal: list[UUID]
) -> None:
    cohort = posthog.add("cohorts", name=COHORT_NAME, description=f"x {MANAGED_TAG}")
    posthog.members[cohort["id"]] = {"p-0", "p-x"}

    plan = sync(posthog.admin(), internal, apply=True)

    assert posthog.members[cohort["id"]] == {"p-0", "p-1"}
    added = [b for m, p, b in posthog.writes if p.endswith("add_persons_to_static_cohort/")]
    assert added == [{"person_ids": ["p-1"]}]
    assert (plan.internal, plan.not_in_posthog) == (3, 1)
    assert plan.changes[0].action == "update"
    assert len(posthog.rows["cohorts"]) == 1


def test_a_dry_run_writes_nothing(posthog: FakePostHog, internal: list[UUID]) -> None:
    plan = sync(posthog.admin(), internal, apply=False)
    assert posthog.writes == []
    assert {c.action for c in plan.changes} == {"create"}
    assert plan.internal == 3


def test_a_changed_definition_updates_in_place(posthog: FakePostHog, internal: list[UUID]) -> None:
    sync(posthog.admin(), internal, apply=True)
    row = next(i for i in posthog.rows["insights"] if i["name"] == "App installs")
    row["description"] = "edited by hand"

    plan = sync(posthog.admin(), internal, apply=True)

    assert [c.name for c in plan.changes if c.action == "update"] == ["App installs"]
    assert len(posthog.rows["insights"]) == 17


def test_only_tagged_items_are_matched_and_a_dropped_insight_leaves_the_dashboard(
    posthog: FakePostHog, internal: list[UUID]
) -> None:
    # Made by hand first, so a match by name alone would find them first.
    someone_elses = posthog.add("dashboards", name="Reliability", tags=[])
    lookalike = posthog.add(
        "insights", name="App installs", tags=[], dashboards=[someone_elses["id"]]
    )
    sync(posthog.admin(), internal, apply=True)
    assert len(posthog.rows["dashboards"]) == 6
    ours = posthog.rows["dashboards"][1]["id"]
    retired = posthog.add("insights", name="Old", tags=[MANAGED_TAG], dashboards=[ours, 999])

    plan = sync(posthog.admin(), internal, apply=True)

    assert retired["dashboards"] == [999]
    assert [c.name for c in plan.changes if c.action == "remove"] == ["Old"]
    assert lookalike["dashboards"] == [someone_elses["id"]]
    assert someone_elses.get("description") is None
    assert {c.action for c in plan.changes if c.name != "Old"} == {"unchanged"}


def test_a_tag_posthog_did_not_keep_stops_the_run(
    posthog: FakePostHog, internal: list[UUID]
) -> None:
    posthog.keep_tags = False
    with pytest.raises(UpstreamError, match="did not keep"):
        sync(posthog.admin(), internal, apply=True)
    assert len(posthog.rows["dashboards"]) == 1


# --- The script -------------------------------------------------------------


def _script() -> ModuleType:
    path = Path(__file__).parents[2] / "scripts" / "sync_posthog_dashboards.py"
    spec = importlib.util.spec_from_file_location("sync_posthog_dashboards", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_script_prints_the_plan(
    posthog: FakePostHog,
    internal: list[UUID],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = _script()

    async def read() -> list[UUID]:
        return internal

    monkeypatch.setattr(script, "read_internal", read)
    assert script.run(False, posthog.admin()) == 0
    out = capsys.readouterr().out.splitlines()
    assert f"cohort create: {COHORT_NAME}" in out
    assert "insight create: Server errors" in out
    assert out[-2:] == ["internal accounts: 3 (1 not in PostHog yet)", "dry run: nothing written"]


def test_a_failure_is_one_line_without_the_key(
    internal: list[UUID],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    script = _script()

    async def read() -> list[UUID]:
        return internal

    def refuse(_: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text=f"key {KEY} lacks cohort:read")

    client = httpx.Client(transport=httpx.MockTransport(refuse))
    admin = PostHogAdmin(api_host=HOST, project_id="42", api_key=KEY, client=client)
    monkeypatch.setattr(script, "read_internal", read)
    caplog.set_level(logging.DEBUG)

    assert script.run(False, admin) == 1

    out = capsys.readouterr().out
    failed = [line for line in out.splitlines() if line.startswith("failed:")]
    assert len(failed) == 1
    assert failed[0].startswith("failed: PostHog GET /cohorts/ failed")
    assert "lacks cohort:read" in failed[0]
    assert KEY not in out
    assert KEY not in caplog.text


def test_an_insight_moved_by_hand_goes_back_to_its_one_managed_dashboard(
    posthog: FakePostHog, internal: list[UUID]
) -> None:
    sync(posthog.admin(), internal, apply=True)
    dashboards = {d["name"]: d["id"] for d in posthog.rows["dashboards"]}
    row = next(i for i in posthog.rows["insights"] if i["name"] == "App installs")
    row["dashboards"] = [dashboards["Engagement"], dashboards["Reliability"], 999]

    plan = sync(posthog.admin(), internal, apply=True)

    assert sorted(row["dashboards"]) == sorted([dashboards["Reliability"], 999])
    assert [c.name for c in plan.changes if c.action == "update"] == ["App installs"]


def test_a_cohort_of_the_same_name_made_by_hand_is_left_alone(
    posthog: FakePostHog, internal: list[UUID]
) -> None:
    theirs = posthog.add("cohorts", name=COHORT_NAME, description="made by hand")
    posthog.members[theirs["id"]] = {"p-x"}

    plan = sync(posthog.admin(), internal, apply=True)

    assert plan.changes[0].action == "create"
    assert posthog.members[theirs["id"]] == {"p-x"}
    ours = posthog.rows["cohorts"][1]
    assert posthog.members[ours["id"]] == {"p-0", "p-1"}
