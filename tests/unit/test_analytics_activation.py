"""Decision 338: the mentor activation funnel's counting, the figures beside
the funnels, and PostHog's profile-link read (a fake transport, no network).
Which rows the database counts for each mentor is the DB suite's to prove."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.dialects import postgresql
from tests.posthog_fakes import body_of, posthog_reader, rows

from app.core.config import Settings
from app.core.errors import AnalyticsUpstreamError
from app.domain.analytics_activation import (
    Activation,
    ApprovedMentor,
    Stood,
    activation_figures,
    activation_steps,
    days_to_bookable,
)
from app.domain.analytics_behaviour import (
    FIGURES,
    FUNNELS,
    MENTEE_ACTIVATED_WITHIN,
    Breakdown,
    Match,
    figure,
)
from app.domain.enums import FunnelFigureUnit, FunnelLayer
from app.infra.clients import posthog_reader as queries
from app.infra.db import analytics_features as features
from app.infra.db.analytics_activation import breakdown_filter, person_filter
from app.main import create_app

APPROVED = dt.datetime(2026, 11, 1, 9, tzinfo=dt.UTC)
DAY = dt.timedelta(days=1)
ADMIN = UUID("0190aaaa-0000-7000-8000-00000000000a")


def stood(since: dt.timedelta, until: dt.timedelta | None = None) -> Stood:
    """A row standing from ``since`` until ``until`` (``None``: still),
    relative to `APPROVED` (negative: before approval)."""
    return Stood(since=APPROVED + since, until=None if until is None else APPROVED + until)


def mentor(
    *,
    hours: dt.timedelta | None = None,
    own_type: dt.timedelta | None = None,
    booked: bool = False,
    delivered: bool = False,
    calendar: bool = False,
    consented: bool = True,
    spans: tuple[list[Stood], list[Stood]] | None = None,
) -> ApprovedMentor:
    """A mentor approved at `APPROVED`; ``hours`` and ``own_type`` are when
    each was set and still stands, or ``spans`` gives both lists."""
    if spans is None:
        spans = (
            [] if hours is None else [stood(hours)],
            [] if own_type is None else [stood(own_type)],
        )
    return ApprovedMentor(
        user_id=uuid4(),
        approved_at=APPROVED,
        hours=spans[0],
        own_types=spans[1],
        booked=booked,
        delivered=delivered,
        calendar=calendar,
        consented=consented,
    )


def test_the_steps_are_the_funnels_keys_each_needing_the_one_before_in_its_layer() -> None:
    mentors = [
        mentor(hours=-DAY, own_type=DAY, booked=True, delivered=True),
        # An own type without hours is not past the hours step.
        mentor(own_type=DAY, booked=True),
        mentor(hours=-DAY),
        # A delivered session never counts without the request before it.
        mentor(delivered=True),
    ]
    counts = activation_steps(mentors)
    assert list(counts) == [s.key for s in FUNNELS["mentor_activation"].steps]
    assert counts == {
        "approved": 4,
        "availability_set": 2,
        "session_type_created": 1,
        "first_booking_received": 2,
        "first_session_delivered": 1,
    }


def test_nobody_approved_counts_zero_everywhere() -> None:
    assert set(activation_steps([]).values()) == {0}


def test_bookable_is_when_both_setup_steps_held_never_before_approval() -> None:
    assert mentor(hours=-3 * DAY, own_type=-DAY).bookable_at == APPROVED
    assert mentor(hours=-DAY, own_type=2 * DAY).bookable_at == APPROVED + 2 * DAY
    assert mentor(hours=3 * DAY, own_type=DAY).bookable_at == APPROVED + 3 * DAY
    assert mentor(hours=DAY).bookable_at is None
    assert mentor(own_type=DAY).bookable_at is None


def test_bookable_needs_hours_and_an_own_type_standing_at_the_same_time() -> None:
    # Hours on day 1, gone on day 2; own type from day 10; hours again on day
    # 20. Not day 10 (the later of the two first ones): nothing overlapped
    # until day 20.
    spans = ([stood(DAY, 2 * DAY), stood(20 * DAY)], [stood(10 * DAY)])
    assert mentor(spans=spans).bookable_at == APPROVED + 20 * DAY
    # Both gone before they ever met: never bookable.
    apart = ([stood(DAY, 2 * DAY)], [stood(10 * DAY, 12 * DAY)])
    assert mentor(spans=apart).bookable_at is None
    # Meeting exactly at an end is not standing together.
    touching = ([stood(DAY, 10 * DAY)], [stood(10 * DAY)])
    assert mentor(spans=touching).bookable_at is None
    # Before approval and still standing: bookable at approval.
    assert mentor(spans=([stood(-5 * DAY)], [stood(-3 * DAY, 4 * DAY)])).bookable_at == APPROVED


def test_days_to_bookable_is_the_median_of_the_bookable_and_null_with_none() -> None:
    bookable = [mentor(hours=-DAY, own_type=d * DAY) for d in (1, 2)]
    # Not bookable: left out, not counted as a wait.
    assert days_to_bookable([*bookable, mentor(hours=-DAY)]) == 1.5
    assert days_to_bookable([mentor(hours=-DAY, own_type=dt.timedelta(hours=3))]) == 0.1
    assert days_to_bookable([mentor(hours=-DAY)]) is None
    assert days_to_bookable([]) is None


def by_key(figures: list[Any]) -> dict[str, Any]:
    return {f.key: f for f in figures}


def test_the_figures_are_measured_over_the_approved() -> None:
    now = Activation(
        mentors=[
            mentor(hours=-DAY, own_type=2 * DAY, calendar=True),
            mentor(hours=-DAY, own_type=4 * DAY),
            mentor(),
        ],
        shared=1,
    )
    before = Activation(mentors=[mentor(hours=-DAY, own_type=DAY, calendar=True)], shared=0)
    # Of three approved, one has analytics off: shares are of the other two.
    off = replace(now.mentors[2], consented=False)
    now = Activation(mentors=[*now.mentors[:2], off], shared=1)
    got = by_key(activation_figures(now, before))
    assert list(got) == ["days_to_bookable", "calendar_connected", "profile_link_shared"]
    assert (got["days_to_bookable"].value.value, got["days_to_bookable"].value.previous) == (
        3.0,
        1.0,
    )
    assert got["calendar_connected"].value.value == 33.3
    assert got["calendar_connected"].value.previous == 100.0
    assert (got["profile_link_shared"].value.value, got["profile_link_shared"].value.previous) == (
        50.0,
        0.0,
    )
    assert [f.unit for f in got.values()] == [
        FunnelFigureUnit.DAYS,
        FunnelFigureUnit.PERCENT,
        FunnelFigureUnit.PERCENT,
    ]


def test_without_compare_every_previous_is_null() -> None:
    now = Activation(mentors=[mentor(hours=-DAY, own_type=DAY, calendar=True)], shared=1)
    got = activation_figures(now, None)
    assert [f.value.value for f in got] == [1.0, 100.0, 100.0]
    assert [f.value.previous for f in got] == [None, None, None]
    assert figure("median_review_days", 2.5, 1.0, compare=False).value.previous is None
    assert figure("median_review_days", 2.5, 1.0, compare=True).value.previous == 1.0


def test_nobody_approved_is_null_never_a_division_error() -> None:
    empty = Activation(mentors=[], shared=0)
    got = activation_figures(empty, empty)
    assert [(f.value.value, f.value.previous) for f in got] == [(None, None)] * 3


def test_nobody_with_analytics_on_nulls_the_share_figure() -> None:
    off = Activation(mentors=[mentor(calendar=True, consented=False)], shared=0)
    got = by_key(activation_figures(off, None))
    assert got["profile_link_shared"].value.value is None
    assert got["calendar_connected"].value.value == 100.0


def test_a_posthog_that_did_not_answer_nulls_only_its_figure() -> None:
    got = by_key(activation_figures(Activation(mentors=[mentor(calendar=True)], shared=None), None))
    assert got["profile_link_shared"].value.value is None
    assert got["calendar_connected"].value.value == 100.0


def test_the_labels_are_as_agreed_with_the_frontend() -> None:
    assert {key: label for key, (label, _) in FIGURES.items()} == {
        "days_to_bookable": "Days to bookable",
        "calendar_connected": "Connected a calendar (of approved)",
        "profile_link_shared": "Shared their profile link (of approved, with analytics on)",
        "median_review_days": "Median days in review",
    }
    assert FIGURES["median_review_days"][1] == FunnelFigureUnit.DAYS


def test_every_breakdown_of_a_database_funnel_reads_a_column() -> None:
    for funnel in FUNNELS.values():
        if funnel.in_database:
            for option in funnel.by.values():
                assert len(breakdown_filter(option)) == len(option.matches)
    for refused in (
        Breakdown((Match("role", "mentor", on="person"),)),
        Breakdown((Match("entry_point", "explore"),)),
        Breakdown(step=0),
    ):
        with pytest.raises(ValueError, match="database"):
            breakdown_filter(refused)


THIRTY_DAYS = 30 * 86400


def test_the_profile_link_read_sends_its_mentors_and_bounds_as_values() -> None:
    first, second = uuid4(), uuid4()
    read, seen = posthog_reader(lambda _: rows(["shared"], [1]))
    approvals = [(first, APPROVED + DAY), (second, APPROVED)]
    assert read.profile_links_shared([approvals], [ADMIN]) == [1]
    query = body_of(seen[0])["query"]
    assert (query["kind"], query["name"]) == ("HogQLQuery", "admin_analytics_profile_links")
    later = int((APPROVED + DAY).timestamp())
    assert query["values"] == {
        "approved_at": [later, int(APPROVED.timestamp())],
        "internal": [str(ADMIN)],
        "link_shared": "link_shared",
        "mentors": [str(first), str(second)],
        "own_flag": "true",
        "profile_kind": "mentor_profile",
        "since": int(APPROVED.timestamp()),
        "until": later + THIRTY_DAYS,
        "within_seconds": THIRTY_DAYS,
    }
    assert str(first) not in query["query"]
    text = " ".join(query["query"].split())
    # Each mentor's share counts from their own approval, for 30 days.
    approved = "{approved_at}[indexOf({mentors}, distinct_id)]"
    assert f"toUnixTimestamp(timestamp) >= {approved}" in text
    assert f"toUnixTimestamp(timestamp) < {approved} + {{within_seconds}}" in text


def test_only_a_share_flagged_as_their_own_counts() -> None:
    # `own` must equal 'true': a visitor's share ('false') and one sent before
    # the flag existed (null, which equals nothing) are out. HogQL cannot run
    # here, so the condition and the value it is held to are what is checked.
    text = " ".join(queries.PROFILE_LINKS_QUERY.split())
    assert "AND toString(properties.own) = {own_flag} AND" in text
    assert queries.NAMES["own_flag"] == "true"


def test_nobody_approved_asks_posthog_nothing() -> None:
    read, seen = posthog_reader(lambda _: rows(["shared"], [9]))
    assert read.profile_links_shared([[], []], [ADMIN]) == [0, 0]
    assert seen == []


def test_one_failure_fails_every_cohort_without_a_second_wait() -> None:
    read, seen = posthog_reader(lambda _: httpx.Response(504))
    cohorts = [[(uuid4(), APPROVED)], [(uuid4(), APPROVED - 30 * DAY)]]
    with pytest.raises(AnalyticsUpstreamError):
        read.profile_links_shared(cohorts, [ADMIN])
    assert len(seen) == 1


def test_a_negated_breakdown_keeps_a_null_as_posthog_does() -> None:
    negated = person_filter(Match("signup_source", "invite", on="person", negate=True))
    assert "IS DISTINCT FROM" in str(negated.compile(dialect=postgresql.dialect()))
    exact = person_filter(Match("signup_source", "invite", on="person"))
    assert " = " in str(exact.compile(dialect=postgresql.dialect()))


def test_the_layer_and_figures_are_published_optional() -> None:
    spec = create_app(Settings(_env_file=None)).openapi()["components"]["schemas"]  # type: ignore[call-arg]
    assert spec["FunnelLayer"]["enum"] == [layer.value for layer in FunnelLayer]
    assert spec["FunnelFigureUnit"]["enum"] == ["days", "percent"]
    figure_read = spec["FunnelFigureRead"]
    assert set(figure_read["properties"]) == {"key", "label", "value", "previous", "unit"}
    assert spec["AnalyticsFunnelRead"]["properties"]["figures"]["items"] == {
        "$ref": "#/components/schemas/FunnelFigureRead"
    }


def test_one_fourteen_days_for_mentee_activation_with_its_two_anchors() -> None:
    assert dt.timedelta(days=14) == MENTEE_ACTIVATED_WITHIN
    assert FUNNELS["mentee_activation"].window_days == MENTEE_ACTIVATED_WITHIN.days
    # Overview reads the same object, not a copy of the number.
    assert features.MENTEE_ACTIVATED_WITHIN is MENTEE_ACTIVATED_WITHIN
