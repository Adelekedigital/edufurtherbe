"""The join window, and what a session becomes once it closes.

**Two windows govern a session and this is the second one.** The *response*
window decides when an unanswered request dies and runs backwards from the start
to ``starts_at - W``; this one decides whether a party was **present**, and it
straddles the start. An earlier draft of the build plan had one value doing both
jobs, and they are not each other's fallback: one applies to
confirmation-required offerings only, the other to every session once confirmed.

``starts_at - 5 minutes`` to ``starts_at + 15 minutes``. Asymmetric, and both
halves are the human behaviour rather than a round number: five minutes early is
somebody arriving, and fifteen late is somebody who was held up rather than
somebody who never came. Later a mentor preference, which is why the two are
separate constants.

**Attendance is client-reported, and there is no second source.** ADR 0004 makes
the calendar a write target and an on-demand free/busy read, so nothing tells
this service that a Meet room had two people in it — and `mentor_stats` already
records that Google Meet's conference records need domain-wide delegation on the
*organiser's* Workspace, which a platform never holds for individual mentors.
The party pressing Join is the signal available, and saying so is what stops a
reader treating `attended` as observed fact.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

from app.domain.enums import SessionStatus

__all__ = [
    "JOIN_CLOSES",
    "JOIN_OPENS",
    "join_window",
    "outcome",
    "window_has_closed",
    "within_join_window",
]

#: How early a party may mark themselves present.
JOIN_OPENS = dt.timedelta(minutes=5)

#: How late. **Also when the session's outcome becomes decidable**, which is the
#: same instant deliberately: an outcome settled before the window shut would
#: brand somebody absent while they still had time to arrive.
JOIN_CLOSES = dt.timedelta(minutes=15)


def join_window(starts_at: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """The half-open interval a party may mark themselves present in.

    Returned as a pair rather than as two calls so the two ends cannot be
    derived from different constants by two callers — the shape the response
    window's own history warns about.
    """
    return starts_at - JOIN_OPENS, starts_at + JOIN_CLOSES


def within_join_window(starts_at: dt.datetime, now: dt.datetime) -> bool:
    """Whether ``now`` is inside the window.

    **Half-open**: the closing instant is already too late, so this and
    :func:`window_has_closed` partition the timeline with no instant belonging to
    both. Without that a settlement running exactly on the boundary could mark a
    party absent in the same second they were still allowed to arrive.
    """
    opens, closes = join_window(starts_at)
    return opens <= now < closes


def window_has_closed(starts_at: dt.datetime, now: dt.datetime) -> bool:
    """Whether the outcome is decidable yet. The exact complement of the upper
    bound above, written as its own function because the settlement asks the
    question in SQL and the two must agree on the boundary."""
    return now >= starts_at + JOIN_CLOSES


def outcome(attendance: Iterable[bool]) -> SessionStatus:
    """``COMPLETED`` when everybody came, ``NO_SHOW`` when anybody did not.

    **The rule is *all*, not *any*, and the asymmetry is the product's.** A
    session one party attended alone did not happen, whichever party it was —
    the mentee sitting in an empty room and the mentor sitting in one are the
    same outcome for the session, and the two are told apart by the
    *participants'* statuses rather than by the session's.

    ``NO_SHOW`` here is not `AttendanceStatus.NO_SHOW`: this one says the session
    did not happen, that one says a named person did not arrive.

    An empty iterable yields ``NO_SHOW``, which is unreachable — every session
    has two participant rows written with it — and is the right answer anyway:
    a session nobody was recorded at is not one that happened.

    **The settlement does not call this**, and that is the one thing here worth
    being uneasy about. It decides the same question set-based in SQL, because a
    per-session loop would make a partial settlement reachable — a session saying
    `completed` while its participants still say `pending`. So the rule exists
    twice, which non-negotiable #8 calls a defect unless the copies are pinned by
    a test that fails when they diverge. They are: this function is the
    *specification*, and `test_the_settlement_agrees_with_the_rule` drives every
    combination through both and compares.
    """
    values = list(attendance)
    return SessionStatus.COMPLETED if values and all(values) else SessionStatus.NO_SHOW
