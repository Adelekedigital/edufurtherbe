"""Session length at load: the column CHECK, and the sessions that recorded none.

Split from `test_sessions_transform.py` by area (the 900-line rule).
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from tests.unit.test_sessions_transform import booking, calendar_row, plan, tracker

from app.domain.transform.sessions import DEFAULT_DURATION_MINUTES

# --------------------------------------------------------------------------
# Duration, and the column CHECK
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "4", "481", "forty-five"])
def test_a_duration_the_check_would_refuse_is_dropped(value: str) -> None:
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
    assert result.duration_from_mentor == ("sb-1",)
    assert not result.dropped


def test_a_blank_booking_takes_its_trackers_recorded_length() -> None:
    """The booking's own tracker recorded a length: that is a fact, not a guess."""
    result = plan(
        [booking(Duration=None, SessionTracker="st-1")],
        [tracker(**{"Session Duration": "20"})],
        calendar=[calendar_row(**{"meetingDuration-TxT": "60"})],
    )

    assert [row.duration_minutes for row in result.sessions] == [20]
    assert result.duration_from_mentor == ()


def test_no_length_and_no_calendar_takes_the_legacy_mode() -> None:
    result = plan([], [tracker(**{"Session Duration": None})], calendar=[])

    assert [row.duration_minutes for row in result.sessions] == [DEFAULT_DURATION_MINUTES]
    assert result.duration_from_default == ("st-1",)
    assert result.duration_from_mentor == ()


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


def test_a_guessed_length_is_for_a_person_to_review() -> None:
    """The loader exits 2, not 0, so a freeze-window runbook never reads a
    guessed length as a clean run. Only the length bucket differs here."""
    loaded = plan([], [tracker(**{"Session Duration": None})], calendar=[calendar_row()])
    guessed = replace(loaded, booking_defaulted=())

    assert guessed.duration_from_mentor == ("st-1",)
    assert guessed.needs_review is True
    assert replace(guessed, duration_from_mentor=()).needs_review is False


# --------------------------------------------------------------------------
# A tracker's own session id (owner, 2026-11-14)
# --------------------------------------------------------------------------


def test_a_tracker_with_no_length_takes_its_bookings_recorded_one() -> None:
    """Three production trackers recorded no length, while the booking their
    session id names (dropped: no initiator) recorded 60, 60 and 30."""
    result = plan(
        [booking(**{"Session Initiator": None, "Duration": "60"})],
        [tracker(SessionID="sb-1", **{"Session Duration": None})],
        calendar=[calendar_row(**{"meetingDuration-TxT": "30"})],
    )

    assert [row.duration_minutes for row in result.sessions] == [60]
    assert result.duration_from_mentor == ()


def test_a_second_tracker_for_a_loaded_booking_is_absorbed_not_quarantined() -> None:
    """Its booking already loaded, with its mentee, through its own tracker. The
    extra tracker is a duplicate record of that session, not missing data."""
    duplicate = tracker(
        bubble_id="st-2",
        SessionID="sb-1",
        Creator="(App admin)",
        **{"Mentee(userdatatype)": None},
    )
    result = plan([booking(SessionTracker="st-1")], [tracker(SessionID="sb-1"), duplicate])

    assert [row.legacy_bubble_id for row in result.sessions] == ["sb-1"]
    assert result.quarantined == ()
    assert "st-2" in result.absorbed_trackers
    assert result.duplicate_trackers == ("st-2",)


def test_a_booking_with_no_back_link_merges_with_the_tracker_naming_it() -> None:
    """The tracker's attendance and length reach the booking, not a guess."""
    result = plan(
        [booking(SessionTracker=None, Duration=None)],
        [tracker(SessionID="sb-1", **{"Session Duration": "20"})],
        calendar=[calendar_row(**{"meetingDuration-TxT": "60"})],
    )

    assert [row.legacy_bubble_id for row in result.sessions] == ["sb-1"]
    assert [row.duration_minutes for row in result.sessions] == [20]
    assert "st-1" in result.absorbed_trackers
    assert result.duration_from_mentor == ()


def test_a_second_tracker_at_a_different_start_is_its_own_session() -> None:
    """Not provably the same session, so it is not thrown away as a duplicate."""
    other = tracker(bubble_id="st-2", SessionID="sb-1", **{"session time": "Sep 9, 2025 1:00 am"})
    result = plan([booking(SessionTracker="st-1")], [tracker(SessionID="sb-1"), other])

    assert sorted(row.legacy_bubble_id for row in result.sessions) == ["sb-1", "st-2"]
    assert result.duplicate_trackers == ()


def test_a_numeric_zero_is_recorded_and_dropped_not_guessed() -> None:
    result = plan([], [tracker(**{"Session Duration": 0})])

    assert not result.sessions
    assert [row.legacy_bubble_id for row in result.dropped] == ["st-1"]


def test_an_impossible_length_on_the_linked_record_is_dropped_not_guessed() -> None:
    """The booking is blank and its tracker recorded 1: a fact the CHECK refuses."""
    result = plan(
        [booking(Duration=None, SessionTracker="st-1")],
        [tracker(**{"Session Duration": "1"})],
    )

    assert "sb-1" in [row.legacy_bubble_id for row in result.dropped]
    assert result.duration_from_mentor == ()
