"""Session length at load: the column CHECK, and the sessions that recorded none.

Split from `test_sessions_transform.py` by area (the 900-line rule).
"""

from __future__ import annotations

import pytest
from tests.unit.test_sessions_transform import booking, calendar_row, plan, tracker

from app.domain.transform.sessions import DEFAULT_DURATION_MINUTES

# --------------------------------------------------------------------------
# Duration, and the column CHECK
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "4", "481", "forty-five"])
def test_a_duration_the_check_would_refuse_is_dropped(value: str | None) -> None:
    """Two dev trackers carry `1` and `4`. PR 2 proved the CHECK refuses them,
    so they are dropped here rather than aborting the load. A value that is
    present but impossible is a fact we cannot carry; a blank one is not."""
    result = plan([], [tracker(**{"Session Duration": value})])

    assert not result.sessions
    assert [row.legacy_bubble_id for row in result.dropped] == ["st-1"]


@pytest.mark.parametrize("blank", ["", None, "  "])
def test_a_tracker_with_no_length_takes_the_mentors_usual_one(blank: str | None) -> None:
    """Owner, 2026-11-14: 32 production sessions recorded no length. They load
    with the mentor's usual length, and each is named in the report."""
    result = plan(
        [],
        [tracker(**{"Session Duration": blank})],
        calendar=[calendar_row(**{"meetingDuration-TxT": "60"})],
    )

    assert [row.duration_minutes for row in result.sessions] == [60]
    assert result.duration_from_mentor == ("st-1",)
    assert not result.dropped


def test_a_booking_with_no_length_takes_the_mentors_usual_one() -> None:
    result = plan(
        [booking(Duration=None)],
        calendar=[calendar_row(**{"meetingDuration-TxT": "30"})],
    )

    assert [row.duration_minutes for row in result.sessions] == [30]
    assert len(result.duration_from_mentor) == 1


def test_no_length_and_no_calendar_takes_the_legacy_mode() -> None:
    result = plan([], [tracker(**{"Session Duration": None})], calendar=[])

    assert [row.duration_minutes for row in result.sessions] == [DEFAULT_DURATION_MINUTES]
    assert result.duration_from_mentor == ("st-1",)


def test_a_recorded_length_is_kept() -> None:
    result = plan(
        [],
        [tracker(**{"Session Duration": "45"})],
        calendar=[calendar_row(**{"meetingDuration-TxT": "60"})],
    )

    assert [row.duration_minutes for row in result.sessions] == [45]
    assert result.duration_from_mentor == ()


def test_quarantine_and_drop_are_not_the_same_bucket() -> None:
    """A quarantined row cannot be attributed; a dropped one is fully
    attributed and fails a constraint. Folding them together buries a CHECK
    violation inside 'unattributable'."""
    result = plan(
        [],
        [
            tracker(bubble_id="st-1", **{"Mentee(userdatatype)": None, "Creator": "(App admin)"}),
            tracker(bubble_id="st-2", **{"Session Duration": "1"}),
        ],
    )

    assert [held.legacy_bubble_id for held in result.quarantined] == ["st-1"]
    assert [row.legacy_bubble_id for row in result.dropped] == ["st-2"]
