"""PRD 31 PR 5 (decision 331): the funnels, event cards, Explore card and log-in
alert, and the PostHog reader behind them.

PostHog is an `httpx.MockTransport`; nothing here reaches a network.
"""

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
from app.infra.clients.posthog_reader import (
    BOOKED_QUERY,
    CARDS_QUERY,
    EXPLORE_QUERY,
    LOGIN_QUERY,
    NOT_INTERNAL,
    ROUTES_QUERY,
    SOURCES_QUERY,
    NullAnalyticsReader,
    PostHogReader,
    analytics_reader_for,
    replay_url_for,
)

HOST = "https://posthog.example.test"
KEY = "phx_personal_key"
WINDOW = Window(
    since=dt.datetime(2026, 11, 1, tzinfo=dt.UTC), until=dt.datetime(2026, 11, 8, tzinfo=dt.UTC)
)
TODAY = dt.date(2026, 11, 10)
#: An internal account (an admin or a demo address), as the database lists it.
ADMIN = UUID("0190aaaa-0000-7000-8000-00000000000a")
INTERNAL = (ADMIN,)
LEFT_OUT = f"NOT has(['{ADMIN}'], distinct_id)"

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture(autouse=True)
def no_process_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in Settings.model_fields:
        monkeypatch.delenv(env_key(name), raising=False)
    yield


def reader(handler: Handler) -> tuple[PostHogReader, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(record))
    return PostHogReader(api_host=HOST + "/", project_id="42", api_key=KEY, client=client), seen


def body_of(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)  # type: ignore[no-any-return]


def hogql_answer(columns: list[str], *rows: list[Any]) -> httpx.Response:
    return httpx.Response(200, json={"columns": columns, "results": [list(r) for r in rows]})


def funnel_answer(*counts: int) -> httpx.Response:
    results = [{"order": i, "count": c, "name": "x"} for i, c in enumerate(counts)]
    # Out of order on purpose: `order` decides, not position.
    return httpx.Response(200, json={"results": list(reversed(results))})


# --------------------------------------------------------------------------
# The funnel definitions
# --------------------------------------------------------------------------


def test_the_five_funnels_with_their_steps_and_windows() -> None:
    assert FUNNEL_NAMES == (
        "signup",
        "booking",
        "mentee_activation",
        "mentor_activation",
        "mentor_application",
    )
    steps = {name: [s.key for s in FUNNELS[name].steps] for name in FUNNEL_NAMES}
    assert steps["signup"] == [
        "started",
        "code_verified",
        "onboarded",
        "first_booking_or_bookable",
        "first_completed",
    ]
    assert steps["booking"] == ["started", "type", "time", "questions", "requested", "completed"]
    assert steps["mentee_activation"] == [
        "signed_up",
        "onboarded",
        "first_request",
        "first_session",
    ]
    assert steps["mentor_application"] == [
        "started",
        "about_you",
        "education",
        "hours",
        "submitted",
        "approved",
    ]
    assert {name: FUNNELS[name].window_days for name in FUNNEL_NAMES} == {
        "signup": 30,
        "booking": 60,
        "mentee_activation": 14,
        "mentor_activation": 30,
        "mentor_application": 30,
    }


def test_each_funnel_offers_only_its_own_breakdowns() -> None:
    assert list(FUNNELS["signup"].by) == [
        "all",
        "mentee",
        "mentor",
        "invite",
        "direct",
        "campaign",
        "migrated",
    ]
    assert list(FUNNELS["booking"].by) == [
        "all",
        "mentor_profile",
        "explore",
        "home",
        "onboarding_guest",
        "rebook_after_review",
    ]
    assert list(FUNNELS["mentee_activation"].by) == ["all", "invite", "direct"]
    assert list(FUNNELS["mentor_activation"].by) == ["all"]
    assert list(FUNNELS["mentor_application"].by) == ["all"]


def test_a_breakdown_is_checked_against_the_funnel() -> None:
    signup = funnel_named("signup")
    assert breakdown_for(signup, None) == "all"
    assert breakdown_for(signup, "migrated") == "migrated"
    with pytest.raises(AnalyticsWindowError) as refused:
        breakdown_for(signup, "explore")  # the booking funnel's, not this one's
    assert refused.value.field_errors[0][0] == "/query/by"
    with pytest.raises(AnalyticsWindowError):
        breakdown_for(funnel_named("mentor_application"), "migrated")
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


# --------------------------------------------------------------------------
# The log-in alert
# --------------------------------------------------------------------------


def test_the_login_alert_fires_over_ten_percent_only() -> None:
    assert login_alert(LoginCodes(attempts=100, failed=10, failed_migrated=0), TODAY) is None
    assert login_alert(LoginCodes(attempts=0, failed=0, failed_migrated=0), TODAY) is None
    fired = login_alert(LoginCodes(attempts=1000, failed=101, failed_migrated=50), TODAY)
    assert fired is not None
    assert fired.key == "login_code_failed"
    assert fired.title == "Log-in codes failing"
    assert fired.body == "10.1% of log-in attempts failed yesterday (alert at 10%)."
    assert fired.link is not None
    assert (fired.link.tab, fired.link.funnel, fired.link.by) == ("funnels", "signup", "migrated")
    assert fired.raised_at == dt.datetime(2026, 11, 10, tzinfo=dt.UTC)


def test_the_login_alert_never_rounds_down_to_the_threshold() -> None:
    fired = login_alert(LoginCodes(attempts=10_000, failed=1_001, failed_migrated=0), TODAY)
    assert fired is not None
    assert fired.body.startswith("10.1% of log-in attempts failed yesterday")


def test_the_login_alert_says_when_most_failures_are_migrated_users() -> None:
    most = login_alert(LoginCodes(attempts=40, failed=10, failed_migrated=6), TODAY)
    assert most is not None
    assert most.body == (
        "25% of log-in attempts failed yesterday (alert at 10%). Most are migrated users."
    )
    half = login_alert(LoginCodes(attempts=40, failed=10, failed_migrated=5), TODAY)
    assert half is not None and "migrated" not in half.body


# --------------------------------------------------------------------------
# Cards and Explore
# --------------------------------------------------------------------------


def figures(**changes: Any) -> BehaviourFigures:
    values: dict[str, Any] = {
        "login": LoginCodes(attempts=200, failed=20, failed_migrated=15),
        "searches": 80,
        "searches_no_results": 20,
        "profile_views_from_search": 30,
        "profile_views": [Counted("explore", 3), Counted("home", 1)],
        "profile_views_total": 8,
        "join_clicked": 12,
        "join_failed": 2,
        "median_minutes_from_start": 2.75,
        "assist_used": 8,
        "assist_accepted": 6,
        "assist_discarded": 2,
        "app_installed": 4,
        "push_allowed_people": 5,
        "active_people": 40,
        "errors": [Counted(f"/r{i}", 10 - i) for i in range(5)],
    }
    return BehaviourFigures(**(values | changes))


def test_the_cards_turn_counts_into_rates() -> None:
    tab = behaviour_tab(figures())
    assert tab.login_codes == {"attempts": 200, "failed_rate": 10.0, "failed_migrated_rate": 75.0}
    assert tab.search == {
        "searches": 80,
        "no_results_rate": 25.0,
        "profile_views_from_search": 30,
    }
    assert [(r.key, r.share) for r in tab.profile_views_by_source] == [
        ("explore", 37.5),
        ("home", 12.5),
    ]
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


# --------------------------------------------------------------------------
# The reader: what it sends
# --------------------------------------------------------------------------


def test_a_funnel_is_a_funnels_query_to_the_private_query_api() -> None:
    read, seen = reader(lambda _: funnel_answer(50, 40, 30, 20, 10))
    assert read.funnel(FUNNELS["signup"], "migrated", WINDOW, INTERNAL) == [50, 40, 30, 20, 10]
    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == f"{HOST}/api/projects/42/query/"
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    body = body_of(request)
    query = body["query"]
    assert query["kind"] == "FunnelsQuery"
    assert query["dateRange"] == {
        "date_from": "2026-11-01T00:00:00Z",
        "date_to": "2026-11-08T00:00:00Z",
        "explicitDate": True,
    }
    assert query["funnelsFilter"]["funnelWindowInterval"] == 30
    assert query["funnelsFilter"]["funnelWindowIntervalUnit"] == "day"
    assert query["filterTestAccounts"] is False
    assert query["properties"] == [
        {"type": "hogql", "key": LEFT_OUT},
        {"type": "person", "key": "signup_source", "operator": "exact", "value": ["migrated"]},
    ]
    events = [node["event"] for node in query["series"]]
    assert events == ["signup_started", None, "onboarding_completed", None, "session_completed"]
    assert query["series"][1]["properties"] == [
        {"type": "hogql", "key": "event IN ('login_code_verified', 'google_oauth_completed')"}
    ]


def test_an_entry_point_narrows_the_first_booking_step_and_requests_are_the_servers() -> None:
    read, seen = reader(lambda _: funnel_answer(6, 5, 4, 3, 2, 1))
    read.funnel(FUNNELS["booking"], "explore", WINDOW, INTERNAL)
    query = body_of(seen[0])["query"]
    assert query["properties"] == [{"type": "hogql", "key": LEFT_OUT}]
    first, *_, requested, _completed = query["series"]
    assert first["properties"] == [
        {"type": "event", "key": "entry_point", "operator": "exact", "value": ["explore"]}
    ]
    assert query["series"][1]["properties"] == [
        {"type": "event", "key": "step", "operator": "exact", "value": ["type"]}
    ]
    assert requested["properties"] == [
        {"type": "event", "key": "session_id", "operator": "is_set", "value": "is_set"}
    ]


def test_mentee_activation_counts_new_mentees_only() -> None:
    read, seen = reader(lambda _: funnel_answer(4, 3, 2, 1))
    read.funnel(FUNNELS["mentee_activation"], "invite", WINDOW, INTERNAL)
    assert body_of(seen[0])["query"]["properties"] == [
        {"type": "hogql", "key": LEFT_OUT},
        {"type": "person", "key": "role", "operator": "exact", "value": ["mentee"]},
        {"type": "person", "key": "is_migrated", "operator": "is_not", "value": ["true"]},
        {"type": "person", "key": "signup_source", "operator": "exact", "value": ["invite"]},
    ]


def test_hogql_reads_carry_their_window_as_values_never_in_the_text() -> None:
    read, seen = reader(
        lambda _: hogql_answer(["attempts", "failed", "failed_migrated"], [9, 3, 2])
    )
    assert read.login_codes(WINDOW, INTERNAL) == LoginCodes(attempts=9, failed=3, failed_migrated=2)
    query = body_of(seen[0])["query"]
    assert query["kind"] == "HogQLQuery"
    assert query["values"] == {
        "failed": "login_code_failed",
        "internal": [str(ADMIN)],
        "logins": ["login_code_verified", "login_code_failed"],
        "since": 1793491200,
        "until": 1794096000,
    }
    assert "{since}" in query["query"] and "{failed}" in query["query"]
    assert "1793491200" not in query["query"]
    assert "login_code_failed" not in query["query"]


def test_every_hogql_read_leaves_internal_accounts_out() -> None:
    for text in (
        LOGIN_QUERY,
        CARDS_QUERY,
        SOURCES_QUERY,
        ROUTES_QUERY,
        EXPLORE_QUERY,
        BOOKED_QUERY,
    ):
        assert "NOT has({internal}, distinct_id)" in text
        assert "is_internal" not in text
    assert NOT_INTERNAL == "NOT has({internal}, distinct_id)"


# --------------------------------------------------------------------------
# The reader: what it reads back
# --------------------------------------------------------------------------

CARD_COLUMNS = [
    "attempts",
    "failed",
    "failed_migrated",
    "searches",
    "searches_no_results",
    "profile_views_from_search",
    "profile_views",
    "join_clicked",
    "join_failed",
    "median_minutes_from_start",
    "assist_used",
    "assist_accepted",
    "assist_discarded",
    "app_installed",
    "push_allowed_people",
    "active_people",
]


def behaviour_handler(request: httpx.Request) -> httpx.Response:
    name = body_of(request)["query"]["name"]
    if name == "admin_analytics_cards":
        return hogql_answer(CARD_COLUMNS, [10, 2, 1, 8, 2, 3, 40, 5, 1, 4.0, 6, 3, 2, 1, 2, 20])
    if name == "admin_analytics_profile_sources":
        # A value the app never sends (spaces, free text) is not shown.
        return hogql_answer(["key", "count"], ["explore", 6], ["a search text", 1], ["home", 2])
    return hogql_answer(["key", "count"], ["/explore", 4], ["/home", 2])


def test_the_cards_are_three_reads() -> None:
    read, seen = reader(behaviour_handler)
    got = read.behaviour(WINDOW, INTERNAL)
    assert len(seen) == 3
    assert got.login == LoginCodes(attempts=10, failed=2, failed_migrated=1)
    assert (got.searches, got.active_people, got.median_minutes_from_start) == (8, 20, 4.0)
    assert got.profile_views == [Counted("explore", 6), Counted("home", 2)]
    assert got.profile_views_total == 40
    assert got.errors == [Counted("/explore", 4), Counted("/home", 2)]


def test_explore_reads_searchers_per_window_and_bookings_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query = body_of(request)["query"]
        if query["name"] == "admin_analytics_explore":
            return hogql_answer(["g", "searched", "led"], [1, 10, 4], [3, 2, 1])
        return hogql_answer(["booked"], [3])

    read, seen = reader(handler)
    windows = [WINDOW, Window(since=WINDOW.since - dt.timedelta(days=7), until=WINDOW.since)]
    windows += [WINDOW]
    got = read.explore(windows, INTERNAL)
    assert got.per_window == [Searchers(10, 4), Searchers(0, 0), Searchers(2, 1)]
    assert got.booked == 3
    explore, booked = (body_of(r)["query"] for r in seen)
    assert explore["values"]["starts"] == [1793491200, 1792886400, 1793491200]
    assert explore["values"]["since"] == 1792886400
    assert booked["values"] == {
        "booked_events": ["mentor_search_performed", "booking_requested"],
        "internal": [str(ADMIN)],
        "requested": "booking_requested",
        "search": "mentor_search_performed",
        "since": 1793491200,
        "until": 1794096000,
        "booked_until": 1794096000 + 7 * 86400,
        "within": 7 * 86400,
    }


@pytest.mark.parametrize(
    "answer",
    [
        lambda _: httpx.Response(503, json={"detail": "down"}),
        lambda _: httpx.Response(401, json={"detail": "bad key"}),
        lambda _: httpx.Response(200, content=b"<html>not json</html>"),
        lambda _: httpx.Response(200, json={"results": [{"order": 0, "count": 1}]}),
        lambda _: httpx.Response(
            200, json={"results": [{"order": i, "count": -1} for i in range(5)]}
        ),
        lambda _: httpx.Response(200, json=[]),
        lambda _: httpx.Response(200, json={"results": [1, 2, 3, 4, 5]}),
        lambda _: httpx.Response(
            200, content=b'{"results": [{"order": 0, "count": Infinity}, 1, 2, 3, 4]}'
        ),
    ],
    ids=[
        "5xx",
        "4xx",
        "not-json",
        "too-few-steps",
        "negative",
        "not-an-object",
        "steps-not-objects",
        "infinite",
    ],
)
def test_any_failure_is_the_one_upstream_error(answer: Handler) -> None:
    read, _ = reader(answer)
    with pytest.raises(AnalyticsUpstreamError):
        read.funnel(FUNNELS["signup"], "all", WINDOW, INTERNAL)


def test_a_transport_failure_or_timeout_is_an_upstream_error_too() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    read, _ = reader(down)
    with pytest.raises(AnalyticsUpstreamError):
        read.login_codes(WINDOW, INTERNAL)
    read, _ = reader(lambda _: hogql_answer(["attempts", "failed"], [1, 2]))
    with pytest.raises(AnalyticsUpstreamError):
        read.login_codes(WINDOW, INTERNAL)


def test_an_infinite_median_is_an_upstream_error_not_a_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if body_of(request)["query"]["name"] == "admin_analytics_cards":
            row = [10, 2, 1, 8, 2, 3, 9, 5, 1, "INF", 6, 3, 2, 1, 2, 20]
            text = json.dumps({"columns": CARD_COLUMNS, "results": [row]}).replace(
                '"INF"', "Infinity"
            )
            return httpx.Response(200, content=text.encode())
        return hogql_answer(["key", "count"])

    read, _ = reader(handler)
    with pytest.raises(AnalyticsUpstreamError):
        read.behaviour(WINDOW, INTERNAL)


def test_the_upstream_error_says_when_to_retry() -> None:
    assert AnalyticsUpstreamError().problem_headers() == {"Retry-After": "60"}


# --------------------------------------------------------------------------
# Choosing the reader
# --------------------------------------------------------------------------


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


def test_the_optional_reads_are_quick_and_the_tab_reads_wait_for_posthog() -> None:
    timeouts: list[dict[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"])
        if body_of(request)["query"]["kind"] == "FunnelsQuery":
            return funnel_answer(5, 4, 3, 2, 1)
        return hogql_answer(["attempts", "failed", "failed_migrated"], [1, 0, 0])

    read, _ = reader(handler)
    read.login_codes(WINDOW, INTERNAL)
    read.funnel(FUNNELS["signup"], "all", WINDOW, INTERNAL)
    quick, full = timeouts
    assert quick["read"] <= 2.0 and quick["connect"] <= 2.0
    assert full["read"] >= 12.0


def test_profile_view_shares_are_of_every_view_including_unshown_ones() -> None:
    sources = [[f"source_{i:02}", 2] for i in range(20)]

    def handler(request: httpx.Request) -> httpx.Response:
        name = body_of(request)["query"]["name"]
        if name == "admin_analytics_cards":
            # 100 views: 40 under the 20 rows shown, the rest past the limit or blank.
            return hogql_answer(CARD_COLUMNS, [0, 0, 0, 0, 0, 0, 100, 0, 0, None, 0, 0, 0, 0, 0, 0])
        if name == "admin_analytics_profile_sources":
            return hogql_answer(["key", "count"], ["", 30], *sources)
        return hogql_answer(["key", "count"])

    read, _ = reader(handler)
    tab = behaviour_tab(read.behaviour(WINDOW, INTERNAL))
    assert len(tab.profile_views_by_source) == 20
    assert {row.share for row in tab.profile_views_by_source} == {2.0}


def test_a_refusal_that_echoes_the_key_never_reaches_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def echo(request: httpx.Request) -> httpx.Response:
        said = request.headers["Authorization"]
        return httpx.Response(401, json={"detail": f"bad credentials: {said}"})

    read, _ = reader(echo)
    with caplog.at_level(logging.DEBUG), pytest.raises(AnalyticsUpstreamError):
        read.login_codes(WINDOW, INTERNAL)
    assert caplog.records
    for record in caplog.records:
        assert KEY not in record.getMessage()
        assert all(KEY not in str(value) for value in vars(record).values())
