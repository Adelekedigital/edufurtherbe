"""Decision 338: the mentor activation funnel's counting, the figures beside
the funnels, and PostHog's profile-link read (a fake transport, no network).
Which rows the database counts for each mentor is the DB suite's to prove."""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import pytest
from tests.posthog_fakes import body_of, posthog_reader, rows

from app.core.config import Settings
from app.domain.analytics_activation import (
    Activation,
    ApprovedMentor,
    activation_figures,
    activation_steps,
    days_to_bookable,
)
from app.domain.analytics_behaviour import FIGURES, FUNNELS, Breakdown, Match, figure
from app.domain.enums import FunnelFigureUnit, FunnelLayer
from app.infra.db.analytics_activation import breakdown_filter
from app.main import create_app

APPROVED = dt.datetime(2026, 11, 1, 9, tzinfo=dt.UTC)
DAY = dt.timedelta(days=1)
ADMIN = UUID("0190aaaa-0000-7000-8000-00000000000a")


def mentor(
    *,
    hours: dt.timedelta | None = None,
    own_type: dt.timedelta | None = None,
    booked: bool = False,
    delivered: bool = False,
    calendar: bool = False,
) -> ApprovedMentor:
    """A mentor approved at `APPROVED`; ``hours`` and ``own_type`` are when
    each was set, relative to it (negative: before approval)."""
    return ApprovedMentor(
        user_id=uuid4(),
        approved_at=APPROVED,
        hours_at=None if hours is None else APPROVED + hours,
        own_type_at=None if own_type is None else APPROVED + own_type,
        booked=booked,
        delivered=delivered,
        calendar=calendar,
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
    got = by_key(activation_figures(now, before))
    assert list(got) == ["days_to_bookable", "calendar_connected", "profile_link_shared"]
    assert (got["days_to_bookable"].value.value, got["days_to_bookable"].value.previous) == (
        3.0,
        1.0,
    )
    assert got["calendar_connected"].value.value == 33.3
    assert got["calendar_connected"].value.previous == 100.0
    assert (got["profile_link_shared"].value.value, got["profile_link_shared"].value.previous) == (
        33.3,
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


def test_a_posthog_that_did_not_answer_nulls_only_its_figure() -> None:
    got = by_key(activation_figures(Activation(mentors=[mentor(calendar=True)], shared=None), None))
    assert got["profile_link_shared"].value.value is None
    assert got["calendar_connected"].value.value == 100.0


def test_the_labels_are_as_agreed_with_the_frontend() -> None:
    assert {key: label for key, (label, _) in FIGURES.items()} == {
        "days_to_bookable": "Days to bookable",
        "calendar_connected": "Connected a calendar (of approved)",
        "profile_link_shared": "Shared their profile link (of approved)",
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


def test_the_profile_link_read_sends_its_mentors_as_values() -> None:
    first, second = uuid4(), uuid4()
    read, seen = posthog_reader(lambda _: rows(["shared"], [1]))
    approvals = [(first, APPROVED + DAY), (second, APPROVED)]
    assert read.profile_links_shared(approvals, [ADMIN]) == 1
    query = body_of(seen[0])["query"]
    assert (query["kind"], query["name"]) == ("HogQLQuery", "admin_analytics_profile_links")
    assert query["values"] == {
        "approved_at": [int((APPROVED + DAY).timestamp()), int(APPROVED.timestamp())],
        "internal": [str(ADMIN)],
        "link_shared": "link_shared",
        "mentors": [str(first), str(second)],
        "profile_kind": "mentor_profile",
        "since": int(APPROVED.timestamp()),
    }
    assert str(first) not in query["query"]
    # Each mentor's share counts only from their own approval.
    assert "{approved_at}[indexOf({mentors}, distinct_id)]" in query["query"]


def test_nobody_approved_asks_posthog_nothing() -> None:
    read, seen = posthog_reader(lambda _: rows(["shared"], [9]))
    assert read.profile_links_shared([], [ADMIN]) == 0
    assert seen == []


def test_the_layer_and_figures_are_published_optional() -> None:
    spec = create_app(Settings(_env_file=None)).openapi()["components"]["schemas"]  # type: ignore[call-arg]
    assert spec["FunnelLayer"]["enum"] == [layer.value for layer in FunnelLayer]
    assert spec["FunnelFigureUnit"]["enum"] == ["days", "percent"]
    figure_read = spec["FunnelFigureRead"]
    assert set(figure_read["properties"]) == {"key", "label", "value", "previous", "unit"}
    assert spec["AnalyticsFunnelRead"]["properties"]["figures"]["items"] == {
        "$ref": "#/components/schemas/FunnelFigureRead"
    }
