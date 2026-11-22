"""Corrections to who and what a legacy session records.

Split from ``test_sessions_transform`` by area (file-size rule): a blank
``Session Initiator`` read from ``Creator`` (decision 363), and attendance the
owner confirmed (decision 365). Fixtures are shared with that module.
"""

from __future__ import annotations

import pytest
from tests.unit.test_sessions_transform import (
    AS_OF,
    MENTEE,
    MENTOR,
    OTHER,
    STRANGER,
    USERS,
    attendance,
    booking,
    calendar_row,
    completed_booking,
    plan,
    tracker,
)

from app.domain.bubble import EXPORT_TIMEZONE
from app.domain.enums import AttendanceStatus, SessionStatus
from app.domain.transform.sessions import TRUSTED_ATTENDANCE, plan_sessions

# --------------------------------------------------------------------------
# A blank `Session Initiator` falls back to `Creator` (decision 363)
# --------------------------------------------------------------------------


def test_a_blank_initiator_is_read_from_a_creator_who_is_a_user() -> None:
    """67 production bookings from 2024 left `Session Initiator` blank and have
    no tracker; `Creator` names the mentee on every one, and on the 82 such
    bookings that also have a tracker it matched the tracker's mentee 82 of 82."""
    result = plan([booking(**{"Session Initiator": None, "Creator": MENTEE})])

    assert not result.dropped
    (row,) = result.sessions
    assert row.legacy_bubble_id == "sb-1"
    assert row.mentee_bubble_id == MENTEE
    assert row.mentor_bubble_id == MENTOR


@pytest.mark.parametrize("creator", ["(App admin)", None, STRANGER, MENTOR])
def test_a_creator_who_is_no_user_or_the_mentor_is_still_dropped(creator: str | None) -> None:
    """A workflow, nobody, somebody not migrated, or the mentor themself is no
    booker: the row is dropped as before rather than given a guess."""
    result = plan([booking(**{"Session Initiator": None, "Creator": creator})])

    assert not result.sessions
    assert [d.reason for d in result.dropped] == ["booking has no mentor or no initiator"]


def test_a_present_initiator_wins_over_creator() -> None:
    result = plan([booking(**{"Creator": OTHER})])

    (row,) = result.sessions
    assert row.mentee_bubble_id == MENTEE


def test_a_blank_initiator_with_a_tracker_is_left_to_the_tracker() -> None:
    """Those sessions are rebuilt from their tracker under its anchor; reading
    the booking too would load the same session twice."""
    result = plan(
        [booking(**{"Session Initiator": None, "Creator": MENTEE, "SessionTracker": "st-1"})],
        [tracker()],
    )

    assert [row.legacy_bubble_id for row in result.sessions] == ["st-1"]


def test_a_blank_initiator_named_by_a_trackers_session_id_is_left_to_the_tracker() -> None:
    """The booking links no tracker, but a tracker names it by `SessionID`: the
    orphan pass rebuilds that session, so the booking must not load it again."""
    result = plan(
        [booking(**{"Session Initiator": None, "Creator": MENTEE, "SessionTracker": None})],
        [tracker(**{"SessionID": "sb-1"})],
    )

    assert [row.legacy_bubble_id for row in result.sessions] == ["st-1"]


# --------------------------------------------------------------------------
# Attendance the owner confirmed (decision 365)
# --------------------------------------------------------------------------


def test_a_trusted_session_loads_completed_with_both_parties_attended() -> None:
    """The legacy app missed joins made outside it (one static meeting link)."""
    result = plan_sessions(
        USERS,
        [completed_booking(sessionStatus="Missed")],
        [tracker(SessionID="sb-1", **attendance("no", "no"))],
        [calendar_row()],
        [],
        export_timezone=EXPORT_TIMEZONE,
        as_of=AS_OF,
        trusted_attendance=frozenset({"sb-1"}),
    )

    assert result.sessions[0].status is SessionStatus.COMPLETED
    assert {row.attendance_status for row in result.participants} == {AttendanceStatus.ATTENDED}
    assert result.events[-1].to_status is SessionStatus.COMPLETED
    assert any("decision 365" in row.detail for row in result.attendance_overrode_status)


def test_an_untrusted_session_with_the_same_tracker_stays_a_no_show() -> None:
    result = plan(
        [completed_booking(sessionStatus="Missed")],
        [tracker(SessionID="sb-1", **attendance("no", "no"))],
        as_of=AS_OF,
    )

    assert result.sessions[0].status is SessionStatus.NO_SHOW


def test_the_trusted_sessions_are_the_two_the_owner_confirmed() -> None:
    """Owner, 22 Nov 2026: 19 Nov 15:30 and 20 Nov 17:00 UTC, one mentor."""
    assert (
        frozenset({"1794652739037x347061153932835200", "1794607744645x451431224989420350"})
        == TRUSTED_ATTENDANCE
    )
