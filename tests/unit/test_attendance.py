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


def test_the_window_straddles_the_start() -> None:
    """Five minutes before to fifteen after, and the asymmetry is the point: it
    is not a symmetric tolerance around the start but two different human facts
    — arriving early, and being held up."""
    assert join_window(STARTS_AT) == (
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
    assert within_join_window(STARTS_AT, STARTS_AT + offset) is inside


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

    assert not (within_join_window(STARTS_AT, now) and window_has_closed(STARTS_AT, now))


@pytest.mark.parametrize(
    ("attendance", "expected"),
    [
        ((True, True), SessionStatus.COMPLETED),
        ((True, False), SessionStatus.NO_SHOW),
        ((False, True), SessionStatus.NO_SHOW),
        ((False, False), SessionStatus.NO_SHOW),
    ],
)
def test_a_session_happened_only_if_everybody_came(
    attendance: tuple[bool, ...], expected: SessionStatus
) -> None:
    """**`all`, not `any`, and the two one-sided cases are both here.**

    A session one party attended alone did not happen, whichever party it was.
    Testing only "both came" and "neither came" would leave `any` passing, and
    `any` is the reading that reports a mentor who turned up to an empty room as
    having delivered a session.
    """
    assert outcome(attendance) is expected


def test_a_session_with_no_participants_did_not_happen() -> None:
    """Unreachable — booking writes two rows — and the right answer anyway.

    Worth pinning because the obvious `all(...)` is `True` for an empty
    iterable, which would report a session nobody was recorded at as completed.
    """
    assert outcome(()) is SessionStatus.NO_SHOW
