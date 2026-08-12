"""The session transform: what merges, what is quarantined, what is refused.

**On watch-fail.** These were written after the module, against the approved
checklist's instruction to write them first — the same deviation
`test_availability_transform` records, and recorded here for the same reason.
The guarantee comes from the mutation batch instead: each test below was
confirmed to go red when the specific rule it pins is broken.

**Records here are canonical, not raw.** They carry ``bubble_id``,
``created_at`` and ``modified_at``, because that is what an adapter produces and
therefore what the transform actually receives. The first draft of the module
read ``Creation Date`` — the *export's* key — and a probe against the raw JSON
files passed while the real pipeline would have nulled every timestamp and
skipped every event. Fixtures that mirror the wrong shape hide exactly that.

Six behaviours have **no instance in the dev export**: a mentor with no calendar
row, a mentor whose calendar rows disagree on duration, a self-booking, an
absent status, an unmapped status, and a tracker whose mentor equals its mentee.
They are built by hand. A path with no fixture is a path that ships untested.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.domain.bubble import EXPORT_TIMEZONE
from app.domain.enums import ActorType, AttendanceStatus, MeetingProvider, SessionStatus
from app.domain.transform.identity import TransformError
from app.domain.transform.sessions import (
    DEFAULT_DURATION_MINUTES,
    SessionPlan,
    meeting_provider,
    plan_sessions,
    room_id,
)

MENTOR = "1720393627919x464416579629646660"
MENTEE = "1751061686104x253709313178114300"
OTHER = "1749506597973x508378565841367300"
STRANGER = "9999999999999x000000000000000000"

USERS: list[dict[str, Any]] = [
    {"bubble_id": MENTOR, "Mentor": "m-1"},
    {"bubble_id": MENTEE},
    {"bubble_id": OTHER},
]

MEET_URL = "https://meet.google.com/ohd-gsnt-rbj"


def booking(**overrides: Any) -> dict[str, Any]:
    """A canonicalised ``SessionBooking`` row — cancelled by default.

    Cancelled because it is 92 of 105 rows in the export, so the default is the
    shape most assertions need rather than the one that reads most tidily.
    """
    record: dict[str, Any] = {
        "bubble_id": "sb-1",
        "\U0001f575Mentor": MENTOR,
        "Session Initiator": MENTEE,
        "SessionDateTime-UTC(Booked date)": "Sep 8, 2025 1:00 am",
        "Duration": "45",
        "sessionStatus": "Canceled",
        "SessionCancel (Y/N)❌": "yes",
        "Canceled By": MENTEE,
        "Session Cancel/Decline Message": "no longer needed",
        "Session topic": "Statement of Purpose",
        "\U0001f4ac Session booking Message": "please help",
        "Meeting venue": MEET_URL,
        "Creator": "(App admin)",
        "created_at": "Sep 6, 2025 11:44 am",
        "modified_at": "Sep 7, 2025 9:00 am",
    }
    record.update(overrides)
    return record


def tracker(**overrides: Any) -> dict[str, Any]:
    """A canonicalised ``SessionTracker`` row — an orphan by default."""
    record: dict[str, Any] = {
        "bubble_id": "st-1",
        "Mentor(this session)": MENTOR,
        "Mentee(userdatatype)": MENTEE,
        "session time": "Sep 8, 2025 1:00 am",
        "Session Duration": "45",
        "Canceled": "no",
        "TrackStatus(mentee)": "yes",
        "TrackStatus(Mentor)": "yes",
        "Last Joined(mentee)": "Sep 8, 2025 1:05 am",
        "Last Joined(Mentor)": "Sep 8, 2025 1:05 am",
        "meetinglink": MEET_URL,
        "Creator": MENTEE,
        "created_at": "Sep 6, 2025 11:44 am",
        "modified_at": "Sep 7, 2025 9:00 am",
    }
    record.update(overrides)
    return record


def calendar_row(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "bubble_id": "cs-1",
        "Creator": MENTOR,
        "meetingDuration-TxT": "45",
        "created_at": "Sep 1, 2025 9:00 am",
        "modified_at": "Sep 1, 2025 9:00 am",
    }
    record.update(overrides)
    return record


def plan(
    bookings: list[dict[str, Any]] | None = None,
    trackers: list[dict[str, Any]] | None = None,
    calendar: list[dict[str, Any]] | None = None,
    users: list[dict[str, Any]] | None = None,
) -> SessionPlan:
    return plan_sessions(
        USERS if users is None else users,
        bookings or [],
        trackers or [],
        [calendar_row()] if calendar is None else calendar,
        export_timezone=EXPORT_TIMEZONE,
    )


# --------------------------------------------------------------------------
# The merge
# --------------------------------------------------------------------------


def test_a_linked_pair_becomes_one_session() -> None:
    """103 pairs must produce 103 sessions, not 206.

    The whole premise of package D3: the tracker is a mirror, not a second
    entity.
    """
    result = plan([booking(SessionTracker="st-1")], [tracker(SessionID="sb-1")])

    assert len(result.sessions) == 1
    assert result.absorbed_trackers == ("st-1",)
    assert not result.quarantined


def test_the_booking_wins_a_start_time_disagreement() -> None:
    """Two dev pairs disagree. The booking is authoritative; the tracker is the
    mirror, so a difference is reported rather than averaged or preferred."""
    result = plan(
        [booking(SessionTracker="st-1")],
        [tracker(SessionID="sb-1", **{"session time": "Sep 8, 2025 2:00 am"})],
    )

    assert result.sessions[0].starts_at == datetime(2025, 9, 8, 5, 0, tzinfo=UTC)
    assert len(result.starts_at_disagreements) == 1


def test_a_booking_with_no_tracker_still_loads() -> None:
    """Two dev bookings have none. They simply carry no attendance."""
    result = plan([booking()])

    assert len(result.sessions) == 1
    assert result.participants == ()


def test_the_start_time_is_read_in_the_export_zone() -> None:
    """`SessionDateTime-UTC` is not UTC despite its name (settled decision #83).

    1:00 am on 8 September is EDT, so 05:00Z. Reading it as UTC would shift
    every migrated session by four or five hours — plausible, wrong, and
    invisible to any row count.
    """
    result = plan([booking()])

    assert result.sessions[0].starts_at == datetime(2025, 9, 8, 5, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Status, and the rule that a cancelled session is never missed
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("legacy", "expected"),
    [
        ("Canceled", SessionStatus.CANCELLED),
        ("Missed", SessionStatus.NO_SHOW),
        ("Declined", SessionStatus.DECLINED),
        ("Completed", SessionStatus.COMPLETED),
        ("Pending", SessionStatus.PENDING_MENTOR_APPROVAL),
    ],
)
def test_every_legacy_status_maps(legacy: str, expected: SessionStatus) -> None:
    result = plan([booking(sessionStatus=legacy, **{"SessionCancel (Y/N)❌": "no"})])

    assert result.sessions[0].status is expected


def test_an_unmapped_status_raises_rather_than_defaulting() -> None:
    """Dev holds five values against production's 1,073 rows.

    Mapping a sixth to a plausible neighbour produces sessions nobody can find
    again, so it raises — and the error carries the anchor, because an error
    that cannot be traced to a row is a report nobody can act on.
    """
    result = plan([booking(sessionStatus="Rescheduled", **{"SessionCancel (Y/N)❌": "no"})])

    assert result.errors
    assert "sb-1" in result.errors[0]
    assert "Rescheduled" in result.errors[0]
    assert not result.sessions


def test_an_absent_status_is_dropped_with_an_anchor() -> None:
    """Absent and unrecognised are different failures — `_resolve_timezone`'s
    rule. A blank is missing data; a wrong value is a mapping gap."""
    result = plan([booking(sessionStatus=None, **{"SessionCancel (Y/N)❌": "no"})])

    assert not result.errors
    assert [row.legacy_bubble_id for row in result.dropped] == ["sb-1"]


def test_a_cancelled_session_is_never_also_missed() -> None:
    """Three dev rows say `Missed` while carrying the cancel flag, a mentor
    canceller and a reason — the sweep overwrote a human decision half an hour
    after the session. Legacy was inconsistent; that stops here."""
    result = plan(
        [booking(sessionStatus="Missed", **{"SessionCancel (Y/N)❌": "yes", "Canceled By": MENTOR})]
    )

    assert result.sessions[0].status is SessionStatus.CANCELLED
    assert len(result.cancellation_overrode_status) == 1
    assert not [event for event in result.events if event.to_status is SessionStatus.NO_SHOW]


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------


def test_a_session_gets_a_creation_and_a_terminal_event() -> None:
    result = plan([booking()])

    assert [event.to_status for event in result.events] == [
        SessionStatus.PENDING_MENTOR_APPROVAL,
        SessionStatus.CANCELLED,
    ]
    assert result.events[0].from_status is None
    assert result.events[0].created_at == datetime(2025, 9, 6, 15, 44, tzinfo=UTC)
    assert result.events[1].created_at == datetime(2025, 9, 7, 13, 0, tzinfo=UTC)


def test_a_pending_session_gets_exactly_one_event() -> None:
    """It was modified and never transitioned. A terminal event would assert a
    transition that did not happen."""
    result = plan([booking(sessionStatus="Pending", **{"SessionCancel (Y/N)❌": "no"})])

    assert len(result.events) == 1
    assert result.events[0].to_status is SessionStatus.PENDING_MENTOR_APPROVAL


def test_a_confirmed_then_missed_session_records_the_state_not_a_third_event() -> None:
    """Four dev rows are accepted and then swept to Missed.

    We know a confirmation happened and cannot place it in time. The terminal
    event therefore carries `from_status = confirmed` — the state we know — and
    no third event is invented to carry a timestamp we do not have.
    """
    result = plan(
        [
            booking(
                sessionStatus="Missed",
                bookingRequestAccepted="yes",
                **{"SessionCancel (Y/N)❌": "no", "Canceled By": None},
            )
        ]
    )

    assert len(result.events) == 2
    assert result.events[1].from_status is SessionStatus.CONFIRMED
    assert result.events[1].to_status is SessionStatus.NO_SHOW
    assert result.unplaceable_confirmations == ("sb-1",)


def test_a_blank_accepted_flag_is_treated_as_absent() -> None:
    """`blank_to_none` makes it `None` on 100 of 105 rows, so `== ""` would
    classify every one of them as present-and-not-yes."""
    result = plan([booking(bookingRequestAccepted=None)])

    assert result.events[1].from_status is SessionStatus.PENDING_MENTOR_APPROVAL
    assert not result.unplaceable_confirmations


def test_a_no_show_is_the_system_and_carries_no_actor() -> None:
    """The sweep marked it, measured firing within half an hour of the session
    on eight of ten rows. Attaching a person would be a claim about who."""
    result = plan(
        [booking(sessionStatus="Missed", **{"SessionCancel (Y/N)❌": "no", "Canceled By": None})]
    )

    terminal = result.events[1]
    assert terminal.to_status is SessionStatus.NO_SHOW
    assert terminal.actor_bubble_id is None
    assert terminal.actor_type is ActorType.SYSTEM


def test_a_cancellation_names_who_did_it() -> None:
    result = plan([booking(**{"Canceled By": OTHER})])

    terminal = result.events[1]
    assert terminal.actor_bubble_id == OTHER
    assert terminal.actor_type is ActorType.USER
    assert terminal.reason_text == "no longer needed"


def test_a_decline_carries_a_reason_and_no_actor() -> None:
    """The one dev declined row has a message and no `Canceled By`. A person
    declined it and we cannot name them, which is not the same as the system."""
    result = plan(
        [
            booking(
                sessionStatus="Declined",
                **{"SessionCancel (Y/N)❌": "no", "Canceled By": None},
            )
        ]
    )

    terminal = result.events[1]
    assert terminal.to_status is SessionStatus.DECLINED
    assert terminal.actor_bubble_id is None
    assert terminal.actor_type is ActorType.USER
    assert terminal.reason_text == "no longer needed"


def test_a_cancellation_timestamped_after_the_session_is_reported() -> None:
    """Right state, wrong clock — said out loud rather than smoothed over."""
    result = plan([booking(modified_at="Sep 9, 2025 1:00 am")])

    assert result.sessions[0].status is SessionStatus.CANCELLED
    assert len(result.cancel_time_after_session) == 1


def test_expiration_produces_no_event() -> None:
    """It is `session time + 15 minutes` — a join-window cutoff. The package
    reads it as an `expired` event, which would invent a lifecycle."""
    result = plan([], [tracker(expiration="Sep 8, 2025 1:15 am")])

    assert not [event for event in result.events if event.to_status is SessionStatus.EXPIRED]


# --------------------------------------------------------------------------
# Orphan trackers
# --------------------------------------------------------------------------


def test_an_orphan_with_a_mentee_becomes_a_session() -> None:
    result = plan([], [tracker()])

    assert len(result.sessions) == 1
    assert result.sessions[0].from_orphan_tracker is True


def test_an_orphan_falls_back_to_creator_for_the_mentee() -> None:
    """`Mentee(userdatatype)` is populated on 31 of 164; where both exist they
    agree on 27 of 27, which is what makes the fallback defensible."""
    result = plan([], [tracker(**{"Mentee(userdatatype)": None, "Creator": OTHER})])

    assert result.sessions[0].mentee_bubble_id == OTHER


def test_an_orphan_with_no_identifiable_mentee_is_quarantined() -> None:
    """88 dev rows. Attributing them would mean inventing a second party."""
    result = plan([], [tracker(**{"Mentee(userdatatype)": None, "Creator": "(App admin)"})])

    assert not result.sessions
    assert [held.legacy_bubble_id for held in result.quarantined] == ["st-1"]
    assert "no mentee" in result.quarantined[0].reason


def test_an_orphan_gets_a_cancel_event_only_when_it_was_cancelled() -> None:
    """`Canceled = 'no'` means nothing definite. There is no creation event
    either — an orphan predates the booking link and its creation semantics are
    unverified."""
    cancelled = plan([], [tracker(Canceled="yes")])
    not_cancelled = plan([], [tracker(Canceled="no")])

    assert [event.to_status for event in cancelled.events] == [SessionStatus.CANCELLED]
    assert not_cancelled.events == ()


def test_a_tracker_whose_parties_are_the_same_user_is_dropped() -> None:
    """No instance in dev; `no_self_booking` would refuse it at load."""
    result = plan([], [tracker(**{"Mentee(userdatatype)": MENTOR})])

    assert not result.sessions
    assert [row.legacy_bubble_id for row in result.dropped] == ["st-1"]


# --------------------------------------------------------------------------
# Duration, and the column CHECK
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "4", "481", "", None, "forty-five"])
def test_a_duration_the_check_would_refuse_is_dropped(value: str | None) -> None:
    """Two dev trackers carry `1` and `4`. PR 2 proved the CHECK refuses them,
    so they are dropped here rather than aborting the load."""
    result = plan([], [tracker(**{"Session Duration": value})])

    assert not result.sessions
    assert [row.legacy_bubble_id for row in result.dropped] == ["st-1"]


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


# --------------------------------------------------------------------------
# Meeting venue and room id
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://meet.google.com/abc-defg-hij", MeetingProvider.GOOGLE_MEET),
        ("https://edufurther.daily.co/oWRCPoeSSTuFebc2lPZV", MeetingProvider.DAILY),
        ("https://example.test/room", MeetingProvider.CUSTOM),
        ("4SbVwwndFF1cxa6TnwuR", None),
        ("", None),
        (None, None),
    ],
)
def test_the_provider_is_derived_from_the_host(
    url: str | None, expected: MeetingProvider | None
) -> None:
    """The package maps these columns to `meeting_provider`; they hold a URL."""
    assert meeting_provider(url) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("tzrmfVi8ZeKwz7dPYpB8", "tzrmfVi8ZeKwz7dPYpB8"),
        ("Google Meet", None),
        ("https://meet.google.com/abc", None),
        ("", None),
    ],
)
def test_a_display_string_never_becomes_a_room_id(raw: str, expected: str | None) -> None:
    """`google/dailyRoomName` reads the literal `Google Meet` on 180 rows."""
    assert room_id(raw) == expected


def test_a_junk_room_name_is_reported_rather_than_stored() -> None:
    result = plan([booking(**{"google/dailyRoomName": "Google Meet"})])

    assert result.sessions[0].external_room_id is None
    assert len(result.junk_room_names) == 1


# --------------------------------------------------------------------------
# Participants
# --------------------------------------------------------------------------


def test_attendance_comes_from_the_tracker() -> None:
    """`TrackStatus` agrees with `Last Joined` on all 267 dev rows, so the
    mapping needs no tie-break."""
    result = plan(
        [booking(SessionTracker="st-1")],
        [tracker(SessionID="sb-1", **{"TrackStatus(mentee)": "no", "Last Joined(mentee)": None})],
    )

    by_role = {row.role.value: row for row in result.participants}
    assert by_role["mentor"].attendance_status is AttendanceStatus.ATTENDED
    assert by_role["mentee"].attendance_status is AttendanceStatus.NO_SHOW
    assert by_role["mentee"].joined_at is None


# --------------------------------------------------------------------------
# Session types
# --------------------------------------------------------------------------


def test_every_mentor_holding_a_session_gets_one_type() -> None:
    """`session_type_id` becomes NOT NULL after migration, so a mentor without
    a type would make that contract migration impossible."""
    result = plan([booking()])

    assert len(result.session_types) == 1
    assert result.session_types[0].duration_minutes == 45


def test_disagreeing_calendar_rows_take_the_modal_duration() -> None:
    """No dev mentor disagrees; production has 44 mentors over 192 rows."""
    result = plan(
        [booking()],
        calendar=[
            calendar_row(bubble_id="cs-1", **{"meetingDuration-TxT": "30"}),
            calendar_row(bubble_id="cs-2", **{"meetingDuration-TxT": "60"}),
            calendar_row(bubble_id="cs-3", **{"meetingDuration-TxT": "60"}),
        ],
    )

    assert result.session_types[0].duration_minutes == 60
    assert len(result.duration_disagreements) == 1


def test_a_mentor_with_no_calendar_row_takes_the_legacy_mode() -> None:
    """No dev mentor needs this, so without the fixture the path ships
    unexercised. 45 is the mode on both sides of the export."""
    result = plan([booking()], calendar=[])

    assert result.session_types[0].duration_minutes == DEFAULT_DURATION_MINUTES
    assert result.duration_defaulted == (MENTOR,)


# --------------------------------------------------------------------------
# Accounting, and what the report may say
# --------------------------------------------------------------------------


def test_both_accounting_identities_hold() -> None:
    """A tracker absorbed into its booking is not *loaded*.

    Counting it as loaded balances the totals while a row has quietly vanished,
    which is the one failure a row count cannot show.

    **Every term of both identities must be reached by this fixture.** The
    bookings identity has two — loaded and dropped — and the dropped one was
    unreachable here until `sb-2` was added: the fixture's only booking loaded,
    so deleting that term left the *full* suite green. Dev cannot supply the
    gap either, because all 105 bookings load and the two out-of-range durations
    are trackers. A regression there would have passed every test, passed every
    dev run, and first failed at the production extract, inside the freeze.

    The direction is fail-safe — a missing term makes rows look *unaccounted*,
    so the transform refuses rather than losing data quietly — but "it fails
    loudly at the worst possible moment" is not a reason to leave it untested.
    """
    result = plan(
        [
            booking(bubble_id="sb-1", SessionTracker="st-1"),
            # Dropped, not loaded: `ck_sessions_no_self_booking` would refuse it.
            booking(bubble_id="sb-2", **{"Session Initiator": MENTOR}),
        ],
        [
            tracker(bubble_id="st-1", SessionID="sb-1"),
            tracker(bubble_id="st-2"),
            tracker(bubble_id="st-3", **{"Mentee(userdatatype)": None, "Creator": "(App admin)"}),
            tracker(bubble_id="st-4", **{"Session Duration": "1"}),
        ],
    )

    assert set(result.accounted_for_bookings()) == set(result.source_booking_ids)
    assert set(result.accounted_for_trackers()) == set(result.source_tracker_ids)

    # Counts, not only sets. A term that returns nothing still satisfies set
    # equality when no fixture row reaches it — which is exactly how the dropped
    # term stayed unpinned.
    assert len(result.accounted_for_bookings()) == 2
    assert len(result.accounted_for_trackers()) == 4

    # **The session count is part of the identity, not a separate concern.**
    # Set equality alone does not catch a tracker that is merged into its
    # booking *and* loaded again on its own: the anchor simply moves from
    # `absorbed` to `loaded`, both totals stay right, and the database gains a
    # duplicate session. Found by a mutation that removed the absorbed record
    # and left every accounting assertion above green.
    assert len(result.sessions) == 2


def test_the_report_never_prints_a_message_body() -> None:
    """`booking_message` and the cancellation reason are the most sensitive
    content in this phase, and a report is pasted into terminals and tickets.

    A sentinel rather than a scan over real values: one dev message is the two
    characters `ok`, which appears inside the word "booking" and makes a
    substring check fail for a reason that has nothing to do with a leak.
    """
    sentinel = "SENSITIVE-MESSAGE-BODY-DO-NOT-PRINT"
    result = plan(
        [
            booking(
                **{
                    "\U0001f4ac Session booking Message": sentinel,
                    "Session Cancel/Decline Message": sentinel + "-2",
                }
            )
        ]
    )

    assert result.sessions[0].booking_message == sentinel
    assert sentinel not in result.report()


def test_the_report_names_every_outcome_a_run_can_have() -> None:
    """A count nobody prints is a count nobody acts on."""
    result = plan(
        [booking(bubble_id="sb-1", SessionTracker="st-1")],
        [
            tracker(bubble_id="st-1", SessionID="sb-1"),
            tracker(bubble_id="st-2", **{"Mentee(userdatatype)": None, "Creator": "(App admin)"}),
            tracker(bubble_id="st-3", **{"Session Duration": "1"}),
        ],
    )
    report = result.report()

    for expected in ("sessions loaded", "trackers absorbed", "quarantined", "dropped"):
        assert expected in report


def test_a_mentor_with_no_user_side_link_is_reported() -> None:
    """A cross-check, not the attribution path — mentors come from the session
    rows themselves. All five dev session mentors carry the link."""
    result = plan(
        [booking(**{"\U0001f575Mentor": STRANGER})], users=[*USERS, {"bubble_id": STRANGER}]
    )

    assert result.mentors_without_link == (STRANGER,)


def test_overlapping_live_windows_are_reported_for_fixing_at_source() -> None:
    """The pre-flight. The exclusion constraint refuses these, so finding them
    at the extract is what makes them fixable while Bubble is still writable."""
    result = plan(
        [
            booking(bubble_id="sb-1", sessionStatus="Pending", **{"SessionCancel (Y/N)❌": "no"}),
            booking(
                bubble_id="sb-2",
                sessionStatus="Pending",
                **{
                    "SessionCancel (Y/N)❌": "no",
                    "Session Initiator": OTHER,
                    "SessionDateTime-UTC(Booked date)": "Sep 8, 2025 1:30 am",
                },
            ),
        ]
    )

    assert len(result.overlapping_live_windows) == 1


def test_two_mentors_may_hold_the_same_window() -> None:
    """The accepting case for the mentor dimension.

    ``sessions_no_mentor_double_booking`` excludes on ``mentor_id`` *and* the
    window; a pre-flight that compared windows alone would report every
    simultaneous session on the platform as a conflict, and the operator would
    be sent to fix data that is correct.

    This was missing. A mutation removing the mentor comparison survived,
    because the only overlap test used *cancelled* bookings — which are not in
    the guarded statuses, so the pre-flight never looked at them at all.
    """
    second_mentor = "1734290858394x940262126235280600"
    result = plan(
        [
            booking(bubble_id="sb-1", sessionStatus="Pending", **{"SessionCancel (Y/N)❌": "no"}),
            booking(
                bubble_id="sb-2",
                sessionStatus="Pending",
                **{
                    "SessionCancel (Y/N)❌": "no",
                    "\U0001f575Mentor": second_mentor,
                    "Session Initiator": OTHER,
                },
            ),
        ],
        users=[*USERS, {"bubble_id": second_mentor, "Mentor": "m-2"}],
    )

    assert len(result.sessions) == 2
    assert not result.overlapping_live_windows


def test_cancelled_sessions_may_share_a_window() -> None:
    """19 dev mentor-and-start pairs are booked more than once, one of them 26
    times. Without this the migration could not import its own source data."""
    result = plan(
        [
            booking(bubble_id="sb-1"),
            booking(bubble_id="sb-2", **{"Session Initiator": OTHER}),
        ]
    )

    assert len(result.sessions) == 2
    assert not result.overlapping_live_windows


def test_a_self_booking_is_dropped() -> None:
    """No instance in dev; `ck_sessions_no_self_booking` would refuse it."""
    result = plan([booking(**{"Session Initiator": MENTOR})])

    assert not result.sessions
    assert [row.legacy_bubble_id for row in result.dropped] == ["sb-1"]


def test_created_by_is_never_set_from_creator() -> None:
    """`Creator` reads `(App admin)` on all 105 dev bookings — a workflow, not
    a person. `SessionRow` therefore has no `created_by` at all: a column that
    could only ever be null is a column that invites someone to fill it."""
    result = plan([booking()])

    assert not hasattr(result.sessions[0], "created_by")


def test_a_transform_error_names_the_row() -> None:
    """An error without an anchor cannot be acted on."""
    with pytest.raises(TransformError) as raised:
        raise TransformError("sb-9", "unmapped sessionStatus 'Wizard'")

    assert raised.value.bubble_id == "sb-9"
