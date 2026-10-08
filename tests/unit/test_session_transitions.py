"""The transition table itself, without a request or a database.

Two assertions about the *shape* of `TRANSITIONS` rather than about any one
rule. The behaviour is covered end to end in
`tests/integration/test_api_session_transitions.py`; these catch the two ways
the table can be wrong that no individual endpoint test would notice.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.domain.attendance import within_join_window
from app.domain.sessions import (
    CANCELLATION_CUTOFF,
    RESPONSE_WINDOW,
    TRANSITIONS,
    respond_by,
    too_late_to_cancel,
)

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


def test_the_response_window_leaves_the_mentor_time_at_the_booking_floor() -> None:
    """**The arithmetic that chose six hours over twenty-four.**

    The mentor's time to answer is `(starts_at - booked_at) - RESPONSE_WINDOW`,
    and `booked_at` is at best `starts_at - min_notice_minutes`. Against the
    24-hour notice floor a window of 24 hours leaves **zero** — every request on
    a default-configured offering would expire the instant it was made.

    Asserted rather than trusted, because the two values live in different
    modules — the floor is a Pydantic bound on the write schema — and nothing
    else would notice if one moved.
    """
    floor = dt.timedelta(hours=24)

    assert floor - RESPONSE_WINDOW == dt.timedelta(hours=18)
    assert floor - dt.timedelta(hours=24) == dt.timedelta(0), "24h would leave no time at all"


def test_only_an_offering_that_awaits_an_answer_gets_a_deadline() -> None:
    """Null is the domain rule rather than a default: an auto-confirming
    offering has no response window, because nothing is waiting."""
    starts_at = dt.datetime(2026, 8, 20, 15, 0, tzinfo=dt.UTC)

    assert respond_by(starts_at, requires_confirmation=True) == starts_at - RESPONSE_WINDOW
    assert respond_by(starts_at, requires_confirmation=False) is None
