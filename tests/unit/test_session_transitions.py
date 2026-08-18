"""The transition table itself, without a request or a database.

Two assertions about the *shape* of `TRANSITIONS` rather than about any one
rule. The behaviour is covered end to end in
`tests/integration/test_api_session_transitions.py`; these catch the two ways
the table can be wrong that no individual endpoint test would notice.
"""

from __future__ import annotations

import datetime as dt

from app.domain.sessions import CANCELLATION_CUTOFF, TRANSITIONS, too_late_to_cancel

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


def test_the_boundary_is_exclusive() -> None:
    """Exactly ten minutes out is still cancellable. Stated because a boundary
    nobody wrote down is a boundary two readers will implement differently."""
    now = dt.datetime(2026, 8, 18, 12, 0, tzinfo=dt.UTC)

    assert not too_late_to_cancel(now + CANCELLATION_CUTOFF, now)
    assert too_late_to_cancel(now + CANCELLATION_CUTOFF - dt.timedelta(seconds=1), now)
