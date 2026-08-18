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

**Every outcome records how it was reached**, in ``session_events.metadata``.
Today that is always ``AttendanceEvidence.REPORTED``, and saying so in the log
is what stops ``completed`` quietly meaning two different things either side of
the first provider integration.

**A recorded arrival is an intention to attend, not an attendance.** Pressing
Join says *I am here now*; it does not say the other party was, or that either
stayed. Two parties can both be recorded present without the session having
happened — one arriving as the other gives up and leaves — and nothing in this
module can see that, because a single instant per party carries no overlap. What
would see it is a repeated signal while a tab is open, from which co-presence
falls out; that is a decision this project has not taken yet, and until it does
``COMPLETED`` means *both parties turned up within the window* rather than *the
session happened*.

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
from enum import StrEnum

from app.domain.enums import SessionStatus

__all__ = [
    "JOIN_CLOSES",
    "JOIN_OPENS",
    "AttendanceEvidence",
    "join_window",
    "outcome",
    "window_has_closed",
    "within_join_window",
]


class AttendanceEvidence(StrEnum):
    """How an attendance outcome was arrived at, recorded with the outcome.

    **Not in ``domain/enums.py``, deliberately.** That module holds the closed
    vocabularies the *database* constrains — every one of them backs a column
    with a ``CHECK``. This one is a value inside ``session_events.metadata``, a
    JSONB field with no constraint, so putting it there would imply a column
    that does not exist and invite somebody to add one.

    **Written before anything reads it**, which is the opposite of this
    project's usual rule and is justified by exactly one thing: it cannot be
    reconstructed. A session settled today cannot later be re-examined for
    whether anybody observed it, so the fact has to be recorded at the moment
    the outcome is decided or it is lost. `respond_by` staying on an answered
    row is the same argument.

    What it buys: a payout rule can one day require ``OBSERVED`` without a
    second status and without re-judging history, and ``completed`` cannot
    quietly come to mean two different things either side of the first provider
    integration.
    """

    #: Both parties pressed Join. Nothing watched the room, so this says they
    #: each *said* they were there — not that they were there together. Every
    #: outcome carries this today.
    REPORTED = "reported"

    #: A provider reported per-participant join and leave, and the two
    #: intervals overlapped. Nothing produces this yet; Daily is where it comes
    #: from, and `docs/daily-spike-guide.md` Q3 is the measurement that decides
    #: whether it is reachable at all.
    OBSERVED = "observed"


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


def outcome(*, mentor_attended: bool, mentee_attended: bool) -> SessionStatus:
    """``COMPLETED`` only when **both named parties** are recorded present.

    **The two parties are named rather than counted**, and that is the
    correction. The first version took an iterable and asked whether anybody in
    it was absent, which is a different question with the same answer in the
    ordinary case and a wrong one at the edges: a session with *one* participant
    row, or with none at all, contains nobody who is absent and was therefore
    reported as `completed`. `sessions` is 1:1 between exactly one mentor and
    one mentee by design (package D4), so the expected set is knowable and there
    is no reason to infer it from the rows that happen to exist.

    **A missing row is ``False``, not unknown.** By the time this is asked the
    join window has shut, so there is nothing further to learn: somebody with no
    attendance record did not record attending.

    **The rule is *both*, not *either*, and the asymmetry is the product's.** A
    session one party attended alone did not happen, whichever party it was —
    the mentee sitting in an empty room and the mentor sitting in one are the
    same outcome for the session, and the two are told apart by the
    *participants'* statuses and by the event's reason code rather than by the
    session's status.

    ``NO_SHOW`` here is not `AttendanceStatus.NO_SHOW`: this one says the session
    did not happen, that one says a named person did not arrive.

    **What it does not decide is whether the two were ever there at the same
    time.** Both parties can be recorded present without the session having
    happened — one arriving as the other leaves — and nothing here can see that,
    because a press of Join records an intention to attend rather than an
    attendance. That is a known limit, not an oversight; see the module
    docstring.

    **The settlement does not call this**, and that is the one thing here worth
    being uneasy about. It decides the same question set-based in SQL, because a
    per-session loop would make a partial settlement reachable — a session saying
    `completed` while its participants still say `pending`. So the rule exists
    twice, which non-negotiable #8 calls a defect unless the copies are pinned by
    a test that fails when they diverge. **They were not pinned well enough:**
    `test_the_settlement_agrees_with_the_rule` drove only sessions created
    through booking, which always have both rows, so it never reached the case
    the two disagreed on. It now drives the missing-row cases too.
    """
    return SessionStatus.COMPLETED if mentor_attended and mentee_attended else SessionStatus.NO_SHOW
