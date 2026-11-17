"""Decision 351: the median time beside every admin funnel, one rule for both
sources (PostHog's conversion times, and per-person times from this database),
and PostHog's time-to-convert read (a fake transport, no network)."""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
import pytest
from tests.posthog_fakes import body_of, funnel_answer, posthog_reader, time_answer
from tests.unit.test_analytics_activation import APPROVED, DAY, mentor
from tests.unit.test_analytics_behaviour import INTERNAL, LEFT_OUT, WINDOW, person

from app.core.errors import AnalyticsUpstreamError
from app.domain.analytics_activation import activation_steps, path_reached
from app.domain.analytics_behaviour import (
    FUNNELS,
    MEDIAN_MIN_PEOPLE,
    Funnel,
    FunnelReading,
    FunnelTimes,
    Step,
    funnel_tab,
    median_times,
    path_end,
    shown_median,
)
from app.domain.enums import FunnelLayer
from app.infra.clients.posthog_reader import NullAnalyticsReader, funnel_query

HOUR = dt.timedelta(hours=1)


def medians(tab: Any) -> list[int | None]:
    return [step.median_seconds_from_previous for step in tab.steps]


def test_the_threshold_is_five_people() -> None:
    assert MEDIAN_MIN_PEOPLE == 5


def test_a_median_is_shown_from_five_people_rounded_to_a_second() -> None:
    assert shown_median(30.4, 5) == 30
    assert shown_median(30.6, 100) == 31
    # Halves away from zero, as every analytics figure (decision 171).
    assert (shown_median(0.5, 5), shown_median(2.5, 5)) == (1, 3)
    assert shown_median(30.4, 4) is None
    assert shown_median(None, 100) is None
    assert shown_median(30.4, None) is None


def test_every_path_step_after_the_first_gets_its_median_and_the_funnel_its_total() -> None:
    times = FunnelTimes(from_previous=(7.0, 30.4, 60.6), to_complete=100.4)
    tab = funnel_tab(FUNNELS["signup"], "all", [10, 6, 5], None, times=times)
    # The first step has nothing before it, whatever the source says.
    assert medians(tab) == [None, 30, 61]
    assert tab.median_seconds_to_complete == 100


def test_fewer_than_five_people_through_a_step_hides_its_median_and_the_total() -> None:
    times = FunnelTimes(from_previous=(None, 30.0, 60.0), to_complete=90.0)
    tab = funnel_tab(FUNNELS["signup"], "all", [10, 5, 4], None, times=times)
    assert medians(tab) == [None, 30, None]
    # The total is of those who reached the last path step: four.
    assert tab.median_seconds_to_complete is None


def test_an_outcome_step_has_no_median_and_the_total_ends_at_the_last_path_step() -> None:
    times = FunnelTimes(from_previous=(None, 5.0, 7.0), to_complete=5.0)
    tab = funnel_tab(FUNNELS["mentee_activation"], "all", [10, 8, 6], None, times=times)
    assert [s.layer for s in tab.steps][-1] is FunnelLayer.SECONDARY
    assert medians(tab) == [None, 5, None]
    assert tab.median_seconds_to_complete == 5


def test_a_step_nothing_tracks_has_no_median_and_no_times_is_all_null() -> None:
    funnel = Funnel(
        name="signup",
        steps=(Step("a", "A", ("x",)), Step("b", "B", ("y",)), Step("c", "C", ())),
        window_days=1,
        note="",
    )
    times = FunnelTimes(from_previous=(None, 4.0), to_complete=9.0)
    tab = funnel_tab(funnel, "all", [9, 8], None, times=times)
    assert medians(tab) == [None, 4, None]
    # The last path step is untracked, so nothing measures the whole path.
    assert tab.median_seconds_to_complete is None
    bare = funnel_tab(FUNNELS["signup"], "all", [10, 9, 8], [1, 1, 1])
    assert medians(bare) == [None, None, None]
    assert bare.median_seconds_to_complete is None


def test_a_path_step_after_an_outcome_is_refused() -> None:
    # It would be timed from the outcome before it.
    outcome = FunnelLayer.SECONDARY
    path = (Step("a", "A", ("x",)), Step("b", "B", ("y",), layer=outcome))
    with pytest.raises(ValueError, match="follows a secondary"):
        Funnel(name="signup", steps=(*path, Step("c", "C", ())), window_days=1, note="")
    assert Funnel(name="signup", steps=path, window_days=1, note="").steps == path


def test_the_path_ends_at_the_last_primary_step_that_is_counted() -> None:
    ends = {name: path_end(funnel) for name, funnel in FUNNELS.items()}
    assert ends == {
        "signup": 2,
        "booking": 5,
        "mentee_activation": 1,
        "mentor_activation": 2,
        "mentor_application": 5,
    }
    alone = Funnel(name="signup", steps=(Step("a", "A", ("x",)),), window_days=1, note="")
    assert path_end(alone) is None
    untracked = Funnel(
        name="signup", steps=(Step("a", "A", ("x",)), Step("b", "B", ())), window_days=1, note=""
    )
    assert path_end(untracked) is None


def test_database_times_are_medians_of_each_persons_own_steps() -> None:
    funnel = FUNNELS["mentor_activation"]
    t0 = APPROVED
    reached = [
        {"approved": t0, "availability_set": t0 + HOUR, "session_type_created": t0 + 3 * HOUR},
        {"approved": t0, "availability_set": t0 + 2 * HOUR, "session_type_created": t0 + 4 * HOUR},
        {"approved": t0, "availability_set": t0 + 4 * HOUR},
        {"approved": t0},
    ]
    times = median_times(funnel, reached)
    # From approval: 1, 2 and 4 hours; then 2 and 2; the path: 3 and 4.
    assert list(times.from_previous) == [None, 7200.0, 7200.0, None, None]
    assert times.to_complete == 3.5 * 3600
    assert median_times(funnel, []) == FunnelTimes(
        from_previous=(None, None, None, None, None), to_complete=None
    )


def test_a_mentor_reaches_each_setup_step_when_it_first_stood_never_before_approval() -> None:
    # Hours from before approval, an own type two days after: bookable at day 2.
    assert path_reached(mentor(hours=-DAY, own_type=2 * DAY)) == {
        "approved": APPROVED,
        "availability_set": APPROVED,
        "session_type_created": APPROVED + 2 * DAY,
    }
    # An own type before the hours: the step needs both, so it is the hours'.
    assert path_reached(mentor(hours=3 * DAY, own_type=DAY)) == {
        "approved": APPROVED,
        "availability_set": APPROVED + 3 * DAY,
        "session_type_created": APPROVED + 3 * DAY,
    }
    assert path_reached(mentor(own_type=DAY)) == {"approved": APPROVED}


def test_the_activation_counts_and_times_are_the_same_people() -> None:
    mentors = [mentor(hours=k * HOUR, own_type=(k + 1) * HOUR) for k in range(6)]
    mentors += [mentor(own_type=DAY), mentor()]
    counts = activation_steps(mentors)
    funnel = FUNNELS["mentor_activation"]
    times = median_times(funnel, [path_reached(m) for m in mentors])
    tab = funnel_tab(funnel, "all", [counts[s.key] for s in funnel.steps], None, times=times)
    assert [s.count.value for s in tab.steps if s.count] == [8, 6, 6, 0, 0]
    # Hours 0-5 hours after approval (median 2.5 h), each own type an hour later.
    assert medians(tab) == [None, 9000, 3600, None, None]
    assert tab.median_seconds_to_complete == 12600


# --- PostHog ------------------------------------------------------------------


def test_posthogs_step_medians_come_with_the_counts() -> None:
    read, _ = posthog_reader(lambda _: funnel_answer(50, 40, 30, medians=(None, 61.5, 3600)))
    assert read.funnel(FUNNELS["signup"], "all", WINDOW, INTERNAL) == FunnelReading(
        counts=[50, 40, 30], medians=[None, 61.5, 3600.0]
    )
    # Nobody entered: zeros, and nothing to time.
    empty, _ = posthog_reader(lambda _: httpx.Response(200, json={"results": []}))
    assert empty.funnel(FUNNELS["signup"], "all", WINDOW, INTERNAL) == FunnelReading(
        counts=[0, 0, 0], medians=[None, None, None]
    )
    # A step without the field has no median, the counts still stand.
    bare, _ = posthog_reader(lambda _: funnel_answer(5, 4, 3))
    assert bare.funnel(FUNNELS["signup"], "all", WINDOW, INTERNAL).medians == [None] * 3


@pytest.mark.parametrize("bad", ["61", -1, float("nan"), True])
def test_a_median_that_is_not_seconds_is_an_unreadable_answer(bad: object) -> None:
    read, _ = posthog_reader(lambda _: funnel_answer(5, 4, 3, medians=(None, bad, 1)))
    with pytest.raises(AnalyticsUpstreamError):
        read.funnel(FUNNELS["signup"], "all", WINDOW, INTERNAL)


def test_time_to_complete_is_posthogs_time_to_convert_to_the_last_path_step() -> None:
    read, seen = posthog_reader(lambda _: time_answer(5400.0))
    funnel = FUNNELS["mentee_activation"]
    assert read.time_to_complete(funnel, "invite", WINDOW, INTERNAL) == 5400.0
    (request,) = seen
    query = body_of(request)["query"]
    steps = funnel_query(funnel, "invite", WINDOW, INTERNAL)
    # The same funnel, people and window as the counts; only the view differs.
    assert {k: v for k, v in query.items() if k != "funnelsFilter"} == {
        k: v for k, v in steps.items() if k != "funnelsFilter"
    }
    assert query["funnelsFilter"] == {
        **steps["funnelsFilter"],
        "funnelVizType": "time_to_convert",
        "funnelFromStep": 0,
        "funnelToStep": 1,
    }
    assert query["properties"][0] == {"type": "hogql", "key": LEFT_OUT}
    assert person("signup_source", "invite") in query["properties"]


def test_time_to_complete_is_null_when_nobody_converted_or_nothing_is_a_path() -> None:
    read, _ = posthog_reader(lambda _: time_answer(None))
    assert read.time_to_complete(FUNNELS["signup"], "all", WINDOW, INTERNAL) is None
    empty, _ = posthog_reader(lambda _: httpx.Response(200, json={"results": []}))
    assert empty.time_to_complete(FUNNELS["signup"], "all", WINDOW, INTERNAL) is None
    alone = Funnel(name="signup", steps=(Step("a", "A", ("x",)),), window_days=1, note="")
    never, seen = posthog_reader(lambda _: time_answer(1.0))
    assert never.time_to_complete(alone, "all", WINDOW, INTERNAL) is None
    assert seen == []


@pytest.mark.parametrize(
    "results", [{"median_conversion_time": "1"}, {"bins": []}, [{"count": 1}], 3]
)
def test_an_unreadable_time_to_convert_answer_is_the_upstream_error(results: object) -> None:
    read, _ = posthog_reader(lambda _: httpx.Response(200, json={"results": results}))
    with pytest.raises(AnalyticsUpstreamError):
        read.time_to_complete(FUNNELS["signup"], "all", WINDOW, INTERNAL)


def _timed_out(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("slow", request=request)


@pytest.mark.parametrize(
    "answer",
    [
        lambda _: httpx.Response(503, json={"detail": "down"}),
        lambda _: httpx.Response(200, content=b"<html>not json</html>"),
        lambda _: httpx.Response(200, content=b'{"results": {"median_conversion_time": Infinity}}'),
        _timed_out,
    ],
    ids=["5xx", "not-json", "infinite", "timeout"],
)
def test_any_failed_time_to_convert_read_is_the_upstream_error(answer: Any) -> None:
    read, _ = posthog_reader(answer)
    with pytest.raises(AnalyticsUpstreamError):
        read.time_to_complete(FUNNELS["signup"], "all", WINDOW, INTERNAL)


def test_time_to_complete_waits_as_long_as_the_counts() -> None:
    timeouts: list[dict[str, float]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"])
        return time_answer(1.0)

    read, _ = posthog_reader(answer)
    read.time_to_complete(FUNNELS["signup"], "all", WINDOW, INTERNAL)
    assert timeouts[0]["read"] >= 12.0


def test_the_null_reader_refuses_time_to_complete() -> None:
    with pytest.raises(AnalyticsUpstreamError):
        NullAnalyticsReader().time_to_complete(FUNNELS["signup"], "all", WINDOW, INTERNAL)
