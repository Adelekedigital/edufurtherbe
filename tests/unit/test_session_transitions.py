"""The transition table itself, without a request or a database.

Two assertions about the *shape* of `TRANSITIONS` rather than about any one
rule. The behaviour is covered end to end in
`tests/integration/test_api_session_transitions.py`; these catch the two ways
the table can be wrong that no individual endpoint test would notice.
"""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest

from app.core.config import NOTICE_FLOOR_MINUTES, RESPONSE_WINDOW_CEILING_HOURS, Settings
from app.domain.attendance import within_join_window
from app.domain.enums import SessionReasonCode, SessionRole, SessionStatus
from app.domain.sessions import (
    CANCELLATION_CUTOFF,
    TRANSITIONS,
    ending_for,
    respond_by,
    response_window,
    role_of,
    too_late_to_cancel,
)
from app.domain.suggestions import SUGGESTION_HOLD

#: The four endpoints in `api/routes/sessions.py`. Written out rather than
#: derived from the table, because deriving it would make this assert that the
#: table equals itself.
ENDPOINTS = {"accept", "decline", "withdraw", "cancel"}


def test_the_table_covers_every_endpoint_and_nothing_else() -> None:
    """A fifth entry with no endpoint is a rule nobody can invoke; a fifth
    endpoint with no entry raises `KeyError` at request time rather than at
    import, which is the worse of the two."""
    assert set(TRANSITIONS) == ENDPOINTS


def test_no_transition_can_reach_a_state_it_starts_from() -> None:
    """A self-transition would make an action idempotent by accident — accepting
    an already-confirmed session would succeed silently, telling a client it had
    just confirmed something when nothing happened."""
    for name, rule in TRANSITIONS.items():
        assert rule.to not in rule.allowed_from, f"{name} can transition to its own start state"


def test_every_permitted_reason_belongs_to_an_actor_who_may_act() -> None:
    """A reason set keyed on a role the action does not permit is unreachable —
    and unreachable permission is the shape that reads as a rule and enforces
    nothing."""
    for name, rule in TRANSITIONS.items():
        assert set(rule.reasons) <= rule.by, f"{name} permits reasons for a role that cannot act"


def test_no_party_may_give_a_reason_only_the_system_writes() -> None:
    """`expired_no_response` and `account_deleted` are written by the expiry
    sweep and by `DELETE /me`. A party sending one would misfile a cancellation
    as a lapse or a deletion in reporting."""
    system_only = {SessionReasonCode.EXPIRED_NO_RESPONSE, SessionReasonCode.ACCOUNT_DELETED}
    for name, rule in TRANSITIONS.items():
        for role, codes in rule.reasons.items():
            assert not codes & system_only, f"{role} may give a system-only reason on {name}"
    # The positive half: the sets hold codes, so the loop compared something.
    assert SessionReasonCode.OTHER in TRANSITIONS["cancel"].reasons[SessionRole.MENTEE]


def test_the_cutoff_is_one_sided() -> None:
    """A session already under way is past cancelling, not freshly cancellable.

    The obvious implementation — `abs(starts_at - now) < CUTOFF` — reads as
    "near the start" and would let a party cancel a session that ran yesterday,
    overwriting what the attendance sweep is there to decide.
    """
    now = dt.datetime(2026, 8, 18, 12, 0, tzinfo=dt.UTC)

    assert too_late_to_cancel(now + dt.timedelta(minutes=5), now)
    assert too_late_to_cancel(now - dt.timedelta(hours=1), now)
    assert not too_late_to_cancel(now + dt.timedelta(minutes=30), now)


def test_the_boundary_belongs_to_joining_not_cancelling() -> None:
    """Exactly ten minutes out is **too late** to cancel; a second earlier is not.

    This used to say the opposite, which was harmless while the join window
    opened at five minutes. Once it could open at ten (#391), that one instant
    was both cancellable and joinable, so one party could be marked present
    while the other released the session. The join window is half-open
    `[start - lead, ...)`, so the cutoff must close at exactly that instant."""
    now = dt.datetime(2026, 8, 18, 12, 0, tzinfo=dt.UTC)

    assert too_late_to_cancel(now + CANCELLATION_CUTOFF, now)
    assert not too_late_to_cancel(now + CANCELLATION_CUTOFF + dt.timedelta(seconds=1), now)


@pytest.mark.parametrize("lead_minutes", range(0, 11))
@pytest.mark.parametrize("seconds_before", [-60, -1, 0, 1, 59, 60, 599, 600, 601, 3600])
def test_no_instant_is_both_cancellable_and_joinable(
    lead_minutes: int, seconds_before: int
) -> None:
    """**Joining and cancelling never overlap**, at every lead the setting allows
    (0 to 10). A session one party has entered must not be one the other can
    still call off."""
    starts_at = dt.datetime(2026, 8, 18, 12, 0, tzinfo=dt.UTC)
    now = starts_at - dt.timedelta(seconds=seconds_before)
    lead = dt.timedelta(minutes=lead_minutes)

    joinable = within_join_window(starts_at, 60, now, opens_before=lead)
    cancellable = not too_late_to_cancel(starts_at, now)

    assert not (joinable and cancellable)


@pytest.mark.parametrize("hours", range(1, RESPONSE_WINDOW_CEILING_HOURS + 1))
def test_every_permitted_window_leaves_the_mentor_time(hours: int) -> None:
    """**The arithmetic behind the setting's ceiling** (decision 278).

    The mentor's time to answer is the notice given less the window, and the
    least notice a request can carry is the 24-hour floor less a held offer's
    two hours. Every window the setting admits must leave four hours of that.
    """
    least_notice = dt.timedelta(minutes=NOTICE_FLOOR_MINUTES) - SUGGESTION_HOLD
    window = response_window(Settings(_env_file=None, response_window_hours=hours))

    assert least_notice - window >= dt.timedelta(hours=4)


def test_the_ceiling_is_where_four_hours_are_left() -> None:
    least_notice = dt.timedelta(minutes=NOTICE_FLOOR_MINUTES) - SUGGESTION_HOLD

    assert least_notice - dt.timedelta(hours=RESPONSE_WINDOW_CEILING_HOURS) == dt.timedelta(hours=4)


def test_only_an_offering_that_awaits_an_answer_gets_a_deadline() -> None:
    """Null is the domain rule rather than a default: an auto-confirming
    offering has no response window, because nothing is waiting."""
    starts_at = dt.datetime(2026, 8, 20, 15, 0, tzinfo=dt.UTC)
    window = dt.timedelta(hours=12)

    assert respond_by(starts_at, window=window, requires_confirmation=True) == starts_at - window
    assert respond_by(starts_at, window=window, requires_confirmation=False) is None


def test_the_window_is_the_setting() -> None:
    assert response_window(Settings(_env_file=None)) == dt.timedelta(hours=12)
    assert response_window(Settings(_env_file=None, response_window_hours=8)) == dt.timedelta(
        hours=8
    )


@pytest.mark.parametrize(
    ("status", "role", "action"),
    [
        (SessionStatus.CONFIRMED, SessionRole.MENTOR, "cancel"),
        (SessionStatus.CONFIRMED, SessionRole.MENTEE, "cancel"),
        (SessionStatus.PENDING_MENTOR_APPROVAL, SessionRole.MENTOR, "decline"),
        (SessionStatus.PENDING_MENTOR_APPROVAL, SessionRole.MENTEE, "withdraw"),
    ],
)
def test_each_open_session_ends_by_the_action_the_table_gives_that_side(
    status: SessionStatus, role: SessionRole, action: str
) -> None:
    """Read off `TRANSITIONS`, so a deleted account ends a session only the way
    that side may: never `accept`, which starts one."""
    assert ending_for(status, role) == action


def test_a_status_nothing_ends_has_no_ending() -> None:
    with pytest.raises(LookupError):
        ending_for(SessionStatus.CANCELLED, SessionRole.MENTOR)


def test_the_role_is_the_side_the_actor_sits_on() -> None:
    mentor, mentee = uuid4(), uuid4()
    assert role_of(mentor_id=mentor, actor_id=mentor) is SessionRole.MENTOR
    assert role_of(mentor_id=mentor, actor_id=mentee) is SessionRole.MENTEE
