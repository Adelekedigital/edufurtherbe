"""The join window and the outcome rule, without a request or a database.

Boundaries and a truth table — the two things that are cheap here and expensive
to assert through an endpoint, because reaching a boundary through HTTP means
moving a session's start rather than the clock.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.domain.attendance import (
    JOIN_CLOSES,
    JOIN_OPENS,
    join_window,
    outcome,
    window_has_closed,
    within_join_window,
)
from app.domain.enums import SessionStatus

STARTS_AT = dt.datetime(2026, 8, 18, 15, 0, tzinfo=dt.UTC)

#: An hour: long enough that the fifteen-minute edge, not the session's end,
#: closes the window. The short case has its own tests below.
HOUR = 60


def test_the_window_straddles_the_start() -> None:
    """Five minutes before to fifteen after, and the asymmetry is the point: it
    is not a symmetric tolerance around the start but two different human facts
    — arriving early, and being held up."""
    assert join_window(STARTS_AT, HOUR) == (
        STARTS_AT - dt.timedelta(minutes=5),
        STARTS_AT + dt.timedelta(minutes=15),
    )


@pytest.mark.parametrize(
    ("offset", "inside"),
    [
        (-JOIN_OPENS - dt.timedelta(seconds=1), False),
        (-JOIN_OPENS, True),
        (dt.timedelta(0), True),
        (JOIN_CLOSES - dt.timedelta(seconds=1), True),
        (JOIN_CLOSES, False),
    ],
)
def test_the_window_is_half_open(offset: dt.timedelta, inside: bool) -> None:
    """Open edge inclusive, closing edge not.

    Stated as a test because a boundary nobody wrote down is one two readers
    implement differently — and here the two readers are `within_join_window`
    and the settlement, which must not both claim the closing instant.
    """
    assert within_join_window(STARTS_AT, HOUR, STARTS_AT + offset) is inside


@pytest.mark.parametrize(
    "offset",
    [-dt.timedelta(hours=1), -JOIN_OPENS, dt.timedelta(0), JOIN_CLOSES, dt.timedelta(hours=1)],
)
def test_joining_and_settling_never_overlap(offset: dt.timedelta) -> None:
    """**The invariant the half-open boundary exists for.**

    No instant may be both joinable and settleable, or a sweep running on the
    boundary could mark somebody absent in the same second they were still
    allowed to arrive.
    """
    now = STARTS_AT + offset

    assert not (
        within_join_window(STARTS_AT, HOUR, now) and window_has_closed(STARTS_AT, HOUR, now)
    )


def test_a_short_session_s_window_closes_when_the_session_ends() -> None:
    """**Arrivals stop when the session does** (owner's decision, 2026-10-08).

    A ten-minute session used to accept an arrival until minute fifteen, five
    minutes after its room had closed: the party was marked present at a session
    already over and handed a token for a room that no longer existed.
    """
    assert join_window(STARTS_AT, 10) == (
        STARTS_AT - JOIN_OPENS,
        STARTS_AT + dt.timedelta(minutes=10),
    )


@pytest.mark.parametrize(
    ("offset", "inside"),
    [
        (dt.timedelta(minutes=10) - dt.timedelta(seconds=1), True),
        (dt.timedelta(minutes=10), False),
        (dt.timedelta(minutes=12), False),
    ],
)
def test_a_short_session_s_window_is_half_open_at_its_end(
    offset: dt.timedelta, inside: bool
) -> None:
    assert within_join_window(STARTS_AT, 10, STARTS_AT + offset) is inside


@pytest.mark.parametrize("minutes", [5, 10, 14, 15, 16, 60])
@pytest.mark.parametrize(
    "offset", [dt.timedelta(minutes=m) for m in (-6, -5, 0, 4, 5, 9, 10, 14, 15, 16, 60)]
)
def test_joining_and_settling_partition_the_timeline_at_every_length(
    minutes: int, offset: dt.timedelta
) -> None:
    """**Exactly one of the two at every instant past the opening**, whatever the
    length. The settlement's SQL and these functions now take the duration, so
    the invariant is restated across lengths on both sides of fifteen minutes,
    not only for the hour the original was written for."""
    now = STARTS_AT + offset
    joinable = within_join_window(STARTS_AT, minutes, now)
    settleable = window_has_closed(STARTS_AT, minutes, now)

    assert not (joinable and settleable)
    if now >= STARTS_AT - JOIN_OPENS:
        assert joinable or settleable


@pytest.mark.parametrize(
    ("mentor", "mentee", "expected"),
    [
        (True, True, SessionStatus.COMPLETED),
        (True, False, SessionStatus.NO_SHOW),
        (False, True, SessionStatus.NO_SHOW),
        (False, False, SessionStatus.NO_SHOW),
    ],
)
def test_a_session_happened_only_if_both_parties_came(
    mentor: bool, mentee: bool, expected: SessionStatus
) -> None:
    """**Both, not either, and the two one-sided cases are both here.**

    A session one party attended alone did not happen, whichever party it was.
    Testing only "both came" and "neither came" would leave `any` passing, and
    `any` is the reading that reports a mentor who turned up to an empty room as
    having delivered a session.
    """
    assert outcome(mentor_attended=mentor, mentee_attended=mentee) is expected


def test_a_missing_record_is_absence_rather_than_doubt() -> None:
    """**The correction, and the case the settlement disagreed on.**

    The first version took an iterable and asked whether anybody *in it* was
    absent — so a session with one participant row, or with none, contained
    nobody absent and came back `completed`. By the time this is asked the join
    window has shut, so a party with no record did not record attending, and the
    parties are named rather than inferred from the rows that happen to exist.
    """
    assert outcome(mentor_attended=True, mentee_attended=False) is SessionStatus.NO_SHOW
    assert outcome(mentor_attended=False, mentee_attended=False) is SessionStatus.NO_SHOW
