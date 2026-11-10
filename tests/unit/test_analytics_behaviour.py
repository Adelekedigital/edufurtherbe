"""PRD 31 PR 5 (decision 331): the funnels, event cards, Explore card and log-in
alert, and the PostHog reader behind them (a fake transport, no network)."""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Callable, Iterator
from typing import Any
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr
from tests.posthog_fakes import (
    CARD_COLUMNS,
    HOST,
    KEY,
    Handler,
    body_of,
    funnel_answer,
    posthog_reader,
    rows,
)

from app.core.config import Settings, env_key
from app.core.errors import AnalyticsUpstreamError, AnalyticsWindowError, NotFoundError
from app.domain.analytics import Period
from app.domain.analytics_behaviour import (
    FUNNEL_NAMES,
    FUNNELS,
    BehaviourFigures,
    Counted,
    ExploreFigures,
    LoginCodes,
    Searchers,
    behaviour_tab,
    breakdown_for,
    explore_feature,
    explore_windows,
    funnel_named,
    funnel_tab,
    login_alert,
)
from app.domain.mentor_stats import Window
from app.infra.clients import posthog_reader as queries
from app.infra.clients.posthog_reader import (
    NOT_INTERNAL,
    NullAnalyticsReader,
    PostHogReader,
    analytics_reader_for,
    replay_url_for,
)

WINDOW = Window(
    since=dt.datetime(2026, 11, 1, tzinfo=dt.UTC), until=dt.datetime(2026, 11, 8, tzinfo=dt.UTC)
)
SINCE, UNTIL, WEEK_BEFORE = 1793491200, 1794096000, 1792886400
TODAY = dt.date(2026, 11, 10)
#: An internal account (an admin or a demo address), as the database lists it.
ADMIN = UUID("0190aaaa-0000-7000-8000-00000000000a")
INTERNAL = (ADMIN,)
LEFT_OUT = f"NOT has(['{ADMIN}'], distinct_id)"
LOGIN = ["attempts", "failed", "failed_migrated"]


@pytest.fixture(autouse=True)
def no_process_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in Settings.model_fields:
        monkeypatch.delenv(env_key(name), raising=False)
    yield


def person(key: str, value: str, operator: str = "exact") -> dict[str, Any]:
    return {"type": "person", "key": key, "operator": operator, "value": [value]}


def event(key: str, value: str) -> dict[str, Any]:
    return {"type": "event", "key": key, "operator": "exact", "value": [value]}


STEPS = {
    "signup": "started code_verified onboarded first_booking_or_bookable first_completed",
    "booking": "started type time questions requested completed",
    "mentee_activation": "signed_up onboarded first_request first_session",
    "mentor_activation": "approved availability_set session_type_created conferencing_connected "
    "first_session",
    "mentor_application": "started about_you education hours submitted approved",
}
WINDOWS = {"signup": 30, "booking": 60, "mentee_activation": 14, "mentor_activation": 30}
BY = {
    "signup": "all mentee mentor invite direct campaign migrated",
    "booking": "all mentor_profile explore home onboarding_guest rebook_after_review",
    "mentee_activation": "all invite direct",
}


@pytest.mark.parametrize("name", STEPS)
def test_each_funnel_has_its_steps_window_and_own_breakdowns(name: str) -> None:
    funnel = FUNNELS[name]  # type: ignore[index]
    assert " ".join(s.key for s in funnel.steps) == STEPS[name]
    assert funnel.window_days == WINDOWS.get(name, 30)
    assert " ".join(funnel.by) == BY.get(name, "all")


def test_the_five_funnels_in_order() -> None:
    names = "signup booking mentee_activation mentor_activation mentor_application"
    assert " ".join(FUNNEL_NAMES) == names


@pytest.mark.parametrize(
    ("name", "by", "refused"),
    [
        ("signup", "explore", True),
        ("mentor_application", "migrated", True),
        ("signup", "migrated", False),
    ],
)
def test_a_breakdown_is_checked_against_the_funnel(name: str, by: str, refused: bool) -> None:
    funnel = funnel_named(name)
    assert breakdown_for(funnel, None) == "all"
    if not refused:
        assert breakdown_for(funnel, by) == by
        return
    with pytest.raises(AnalyticsWindowError) as error:
        breakdown_for(funnel, by)
    assert error.value.field_errors[0][0] == "/query/by"


def test_an_unknown_funnel_is_not_found() -> None:
    with pytest.raises(NotFoundError):
        funnel_named("sign_up")


def test_mentee_activation_starts_when_sign_up_does() -> None:
    first = FUNNELS["mentee_activation"].steps[0]
    assert (first.key, first.label, first.events) == ("signed_up", "Signed up", ("signup_started",))
    assert first.events == FUNNELS["signup"].steps[0].events


def test_only_a_trailing_step_is_untracked() -> None:
    for funnel in FUNNELS.values():
        tracked = [bool(step.events) for step in funnel.steps]
        assert tracked == sorted(tracked, reverse=True), funnel.name
    assert [s.key for s in FUNNELS["mentor_activation"].tracked][-1] == "conferencing_connected"


def test_a_funnel_tab_compares_and_leaves_an_untracked_step_null() -> None:
    funnel = FUNNELS["mentor_activation"]
    tab = funnel_tab(funnel, "all", [10, 8, 6, 4], [9, 7, 5, 3])
    assert [s.count.value if s.count else None for s in tab.steps] == [10, 8, 6, 4, None]
    assert [s.count.previous if s.count else None for s in tab.steps] == [9, 7, 5, 3, None]
    alone = funnel_tab(funnel, "all", [10, 8, 6, 4], None)
    assert alone.steps[0].count is not None and alone.steps[0].count.previous is None
    assert tab.note == "Bookable target: 7 days from approval."


@pytest.mark.parametrize(("attempts", "failed"), [(100, 10), (0, 0)])
def test_the_login_alert_is_quiet_at_or_under_ten_percent(attempts: int, failed: int) -> None:
    assert (
        login_alert(LoginCodes(attempts=attempts, failed=failed, failed_migrated=0), TODAY) is None
    )


def test_the_login_alert_fires_over_ten_percent() -> None:
    fired = login_alert(LoginCodes(attempts=1000, failed=101, failed_migrated=50), TODAY)
    assert fired is not None
    assert (fired.key, fired.title) == ("login_code_failed", "Log-in codes failing")
    assert fired.body == "10.1% of log-in attempts failed yesterday (alert at 10%)."
    assert fired.link is not None
    assert (fired.link.tab, fired.link.funnel, fired.link.by) == ("funnels", "signup", "migrated")
    assert fired.raised_at == dt.datetime(2026, 11, 10, tzinfo=dt.UTC)


@pytest.mark.parametrize(
    ("attempts", "failed", "migrated", "body"),
    [
        (10_000, 1_001, 0, "10.1% of log-in attempts failed yesterday (alert at 10%)."),
        (
            40,
            10,
            6,
            "25% of log-in attempts failed yesterday (alert at 10%). Most are migrated users.",
        ),
        (40, 10, 5, "25% of log-in attempts failed yesterday (alert at 10%)."),
    ],
    ids=["never-rounds-down-to-the-threshold", "most-migrated", "half-migrated"],
)
def test_the_login_alert_body(attempts: int, failed: int, migrated: int, body: str) -> None:
    fired = login_alert(
        LoginCodes(attempts=attempts, failed=failed, failed_migrated=migrated), TODAY
    )
    assert fired is not None and fired.body == body


def test_the_cards_turn_counts_into_rates() -> None:
    tab = behaviour_tab(
        BehaviourFigures(
            login=LoginCodes(attempts=200, failed=20, failed_migrated=15),
            searches=80,
            searches_no_results=20,
            profile_views_from_search=30,
            profile_views=[Counted("explore", 3), Counted("home", 1)],
            profile_views_total=8,
            join_clicked=12,
            join_failed=2,
            median_minutes_from_start=2.75,
            assist_used=8,
            assist_accepted=6,
            assist_discarded=2,
            app_installed=4,
            push_allowed_people=5,
            active_people=40,
            errors=[Counted(f"/r{i}", 10 - i) for i in range(5)],
        )
    )
    assert tab.login_codes == {"attempts": 200, "failed_rate": 10.0, "failed_migrated_rate": 75.0}
    assert tab.search == {"searches": 80, "no_results_rate": 25.0, "profile_views_from_search": 30}
    shares = [(r.key, r.share) for r in tab.profile_views_by_source]
    assert shares == [("explore", 37.5), ("home", 12.5)]
    assert tab.joining["median_minutes_from_start"] == 2.8
    assert tab.writing_assist == {"used": 8, "accepted_rate": 75.0, "discarded_rate": 25.0}
    assert tab.app == {"installed": 4, "push_allowed_rate": 12.5}
    assert [r.key for r in tab.errors_by_screen] == ["/r0", "/r1", "/r2"]


def test_the_explore_card_is_searchers_who_went_on_over_searchers() -> None:
    period = Period(first=dt.date(2026, 11, 1), last=dt.date(2026, 11, 8))
    windows = explore_windows(period)
    assert windows[0] == period.window and windows[1] == period.previous().window
    assert len(windows) == 10
    per = [Searchers(10, 4), Searchers(8, 2), *([Searchers(0, 0)] * 7), Searchers(2, 1)]
    card = explore_feature(ExploreFigures(per_window=per, booked=3), compare=True)
    assert card.key == "explore"
    assert card.value is not None and (card.value.value, card.value.previous) == (40.0, 25.0)
    assert card.spark == [None] * 7 + [50.0]
    assert card.contribution == 3
    off = explore_feature(ExploreFigures(per_window=per, booked=3), compare=False)
    assert off.value is not None and off.value.previous is None


def test_a_funnel_is_a_funnels_query_to_the_private_query_api() -> None:
    read, seen = posthog_reader(lambda _: funnel_answer(50, 40, 30, 20, 10))
    assert read.funnel(FUNNELS["signup"], "migrated", WINDOW, INTERNAL) == [50, 40, 30, 20, 10]
    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == f"{HOST}/api/projects/42/query/"
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    query = body_of(request)["query"]
    assert query["kind"] == "FunnelsQuery"
    assert query["dateRange"] == {
        "date_from": "2026-11-01T00:00:00Z",
        "date_to": "2026-11-08T00:00:00Z",
        "explicitDate": True,
    }
    assert query["funnelsFilter"]["funnelWindowInterval"] == 30
    assert query["funnelsFilter"]["funnelWindowIntervalUnit"] == "day"
    assert query["filterTestAccounts"] is False
    left_out = {"type": "hogql", "key": LEFT_OUT}
    assert query["properties"] == [left_out, person("signup_source", "migrated")]
    events = [node["event"] for node in query["series"]]
    assert events == ["signup_started", None, "onboarding_completed", None, "session_completed"]
    either = "event IN ('login_code_verified', 'google_oauth_completed')"
    assert query["series"][1]["properties"] == [{"type": "hogql", "key": either}]


def test_an_entry_point_narrows_the_first_booking_step_and_requests_are_the_servers() -> None:
    read, seen = posthog_reader(lambda _: funnel_answer(6, 5, 4, 3, 2, 1))
    read.funnel(FUNNELS["booking"], "explore", WINDOW, INTERNAL)
    query = body_of(seen[0])["query"]
    assert query["properties"] == [{"type": "hogql", "key": LEFT_OUT}]
    first, step, *_, requested, _completed = query["series"]
    assert first["properties"] == [event("entry_point", "explore")]
    assert step["properties"] == [event("step", "type")]
    server = {"type": "event", "key": "session_id", "operator": "is_set", "value": "is_set"}
    assert requested["properties"] == [server]


def test_mentee_activation_counts_new_mentees_only() -> None:
    read, seen = posthog_reader(lambda _: funnel_answer(4, 3, 2, 1))
    read.funnel(FUNNELS["mentee_activation"], "invite", WINDOW, INTERNAL)
    assert body_of(seen[0])["query"]["properties"] == [
        {"type": "hogql", "key": LEFT_OUT},
        person("role", "mentee"),
        person("is_migrated", "true", "is_not"),
        person("signup_source", "invite"),
    ]


def test_hogql_reads_carry_their_window_as_values_never_in_the_text() -> None:
    read, seen = posthog_reader(lambda _: rows(LOGIN, [9, 3, 2]))
    assert read.login_codes(WINDOW, INTERNAL) == LoginCodes(attempts=9, failed=3, failed_migrated=2)
    query = body_of(seen[0])["query"]
    assert query["kind"] == "HogQLQuery"
    assert query["values"] == {
        "failed": "login_code_failed",
        "internal": [str(ADMIN)],
        "logins": ["login_code_verified", "login_code_failed"],
        "since": SINCE,
        "until": UNTIL,
    }
    assert "{since}" in query["query"] and "{failed}" in query["query"]
    assert str(SINCE) not in query["query"]
    assert "login_code_failed" not in query["query"]


@pytest.mark.parametrize(
    "name",
    [
        "LOGIN_QUERY",
        "CARDS_QUERY",
        "SOURCES_QUERY",
        "ROUTES_QUERY",
        "EXPLORE_QUERY",
        "BOOKED_QUERY",
    ],
)
def test_every_hogql_read_leaves_internal_accounts_out(name: str) -> None:
    text = getattr(queries, name)
    assert "NOT has({internal}, distinct_id)" in text
    assert "is_internal" not in text
    assert NOT_INTERNAL == "NOT has({internal}, distinct_id)"


def cards_handler(row: list[Any], *sources: list[Any]) -> Handler:
    """The cards row, the profile sources, then failed routes for anything else."""

    def answer(request: httpx.Request) -> httpx.Response:
        name = body_of(request)["query"]["name"]
        if name == "admin_analytics_cards":
            return rows(CARD_COLUMNS, row)
        if name == "admin_analytics_profile_sources":
            return rows(["key", "count"], *sources)
        return rows(["key", "count"], ["/explore", 4], ["/home", 2])

    return answer


def test_the_cards_are_three_reads() -> None:
    # A value the app never sends (spaces, free text) is not shown.
    sources = (["explore", 6], ["a search text", 1], ["home", 2])
    row = [10, 2, 1, 8, 2, 3, 40, 5, 1, 4.0, 6, 3, 2, 1, 2, 20]
    read, seen = posthog_reader(cards_handler(row, *sources))
    got = read.behaviour(WINDOW, INTERNAL)
    assert len(seen) == 3
    assert got.login == LoginCodes(attempts=10, failed=2, failed_migrated=1)
    assert (got.searches, got.active_people, got.median_minutes_from_start) == (8, 20, 4.0)
    assert got.profile_views == [Counted("explore", 6), Counted("home", 2)]
    assert got.profile_views_total == 40
    assert got.errors == [Counted("/explore", 4), Counted("/home", 2)]


def test_profile_view_shares_are_of_every_view_including_unshown_ones() -> None:
    # 100 views: 40 under the 20 rows shown, the rest past the limit or blank.
    row = [0, 0, 0, 0, 0, 0, 100, 0, 0, None, 0, 0, 0, 0, 0, 0]
    sources = [["", 30], *([f"source_{i:02}", 2] for i in range(20))]
    read, _ = posthog_reader(cards_handler(row, *sources))
    tab = behaviour_tab(read.behaviour(WINDOW, INTERNAL))
    assert len(tab.profile_views_by_source) == 20
    assert {row.share for row in tab.profile_views_by_source} == {2.0}


def test_explore_reads_searchers_per_window_and_bookings_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if body_of(request)["query"]["name"] == "admin_analytics_explore":
            return rows(["g", "searched", "led"], [1, 10, 4], [3, 2, 1])
        return rows(["booked"], [3])

    read, seen = posthog_reader(handler)
    before = Window(since=WINDOW.since - dt.timedelta(days=7), until=WINDOW.since)
    got = read.explore([WINDOW, before, WINDOW], INTERNAL)
    assert got.per_window == [Searchers(10, 4), Searchers(0, 0), Searchers(2, 1)]
    assert got.booked == 3
    explore, booked = (body_of(r)["query"] for r in seen)
    assert explore["values"]["starts"] == [SINCE, WEEK_BEFORE, SINCE]
    assert explore["values"]["since"] == WEEK_BEFORE
    assert booked["values"] == {
        "booked_events": ["mentor_search_performed", "booking_requested"],
        "internal": [str(ADMIN)],
        "requested": "booking_requested",
        "search": "mentor_search_performed",
        "since": SINCE,
        "until": UNTIL,
        "booked_until": UNTIL + 7 * 86400,
        "within": 7 * 86400,
    }


def _status(code: int, **body: Any) -> Handler:
    return lambda _: httpx.Response(code, **body)


def _timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectTimeout("timed out", request=request)


INF_ROW = json.dumps({"columns": CARD_COLUMNS, "results": [[0] * 9 + ["INF"] + [0] * 6]})


FAILURES: dict[str, tuple[Handler, str]] = {
    "5xx": (_status(503, json={"detail": "down"}), "funnel"),
    "4xx": (_status(401, json={"detail": "bad key"}), "funnel"),
    "not-json": (_status(200, content=b"<html>not json</html>"), "funnel"),
    "too-few-steps": (_status(200, json={"results": [{"order": 0, "count": 1}]}), "funnel"),
    "negative": (_status(200, json={"results": [{"order": 0, "count": -1}] * 5}), "funnel"),
    "not-an-object": (_status(200, json=[]), "funnel"),
    "steps-not-objects": (_status(200, json={"results": [1, 2, 3, 4, 5]}), "funnel"),
    "infinite-count": (
        _status(200, content=b'{"results": [{"count": Infinity}, 1, 2, 3, 4]}'),
        "funnel",
    ),
    "transport-timeout": (_timeout, "login_codes"),
    "missing-column": (lambda _: rows(["attempts", "failed"], [1, 2]), "login_codes"),
    "infinite-median": (
        _status(200, content=INF_ROW.replace('"INF"', "Infinity").encode()),
        "behaviour",
    ),
}


@pytest.mark.parametrize("case", FAILURES)
def test_any_failure_is_the_one_upstream_error(case: str) -> None:
    answer, read = FAILURES[case]
    reader, _ = posthog_reader(answer)
    calls: dict[str, Callable[[], object]] = {
        "funnel": lambda: reader.funnel(FUNNELS["signup"], "all", WINDOW, INTERNAL),
        "login_codes": lambda: reader.login_codes(WINDOW, INTERNAL),
        "behaviour": lambda: reader.behaviour(WINDOW, INTERNAL),
    }
    with pytest.raises(AnalyticsUpstreamError):
        calls[read]()


def test_the_upstream_error_says_when_to_retry() -> None:
    assert AnalyticsUpstreamError().problem_headers() == {"Retry-After": "60"}


def test_the_optional_reads_are_quick_and_the_tab_reads_wait_for_posthog() -> None:
    timeouts: list[dict[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"])
        if body_of(request)["query"]["kind"] == "FunnelsQuery":
            return funnel_answer(5, 4, 3, 2, 1)
        return rows(LOGIN, [1, 0, 0])

    read, _ = posthog_reader(handler)
    read.login_codes(WINDOW, INTERNAL)
    read.funnel(FUNNELS["signup"], "all", WINDOW, INTERNAL)
    quick, full = timeouts
    assert quick["read"] <= 2.0 and quick["connect"] <= 2.0
    assert full["read"] >= 12.0


def test_a_refusal_that_echoes_the_key_never_reaches_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def echo(request: httpx.Request) -> httpx.Response:
        said = request.headers["Authorization"]
        return httpx.Response(401, json={"detail": f"bad credentials: {said}"})

    read, _ = posthog_reader(echo)
    with caplog.at_level(logging.DEBUG), pytest.raises(AnalyticsUpstreamError):
        read.login_codes(WINDOW, INTERNAL)
    assert caplog.records
    for record in caplog.records:
        assert KEY not in record.getMessage()
        assert all(KEY not in str(value) for value in vars(record).values())


def settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


API = {"analytics_api_host": HOST, "analytics_project_id": "42", "analytics_api_key": KEY}


def test_the_reader_needs_the_private_host_project_and_key() -> None:
    assert isinstance(analytics_reader_for(settings(**API)), PostHogReader)
    assert replay_url_for(settings(**API)) == f"{HOST}/project/42/replay/home"
    for missing in API:
        partial = {k: v for k, v in API.items() if k != missing}
        assert isinstance(analytics_reader_for(settings(**partial)), NullAnalyticsReader)
        assert replay_url_for(settings(**partial)) is None
    # The id is written into the path, so it must be a number.
    odd = settings(**(API | {"analytics_project_id": "42/../1"}))
    assert isinstance(analytics_reader_for(odd), NullAnalyticsReader)
    blank = settings(**(API | {"analytics_api_key": SecretStr(" ")}))
    assert isinstance(analytics_reader_for(blank), NullAnalyticsReader)


def test_the_null_reader_refuses_every_read() -> None:
    null = NullAnalyticsReader()
    for read in (
        lambda: null.funnel(FUNNELS["signup"], "all", WINDOW, INTERNAL),
        lambda: null.behaviour(WINDOW, INTERNAL),
        lambda: null.explore([WINDOW], INTERNAL),
        lambda: null.login_codes(WINDOW, INTERNAL),
    ):
        with pytest.raises(AnalyticsUpstreamError):
            read()
