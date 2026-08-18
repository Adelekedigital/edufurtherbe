"""Which state a session may move to, who may move it, and why.

Pure, and in ``domain`` because every line here is a product rule rather than a
storage detail. ``SessionStatus``'s own docstring already says the transitions
are enforced "at the endpoint that writes the status" and that nothing in the
schema holds them; this module is that rule, written once so the four endpoints
are four names for one table rather than four implementations.

**The ten-minute cancellation cutoff is here rather than in a trigger**, and the
build plan floated a trigger because the rule is time-relative and a ``CHECK``
predicate must be ``IMMUTABLE`` while ``now()`` is ``STABLE``. That reasoning
correctly rules out a ``CHECK`` and does not by itself argue for a trigger. The
project already settled where a rule like this lives: *the database refuses what
is impossible; the application refuses what is disallowed* — which is why the
24-hour booking notice is a Pydantic bound over a wide sanity ``CHECK`` rather
than a constraint carrying the product's current mind. A cutoff the product will
want to change is the same shape, and a trigger would make every change a
migration.

**The reason codes are restricted per actor, and that is authorization rather
than tidiness.** ``SessionReasonCode``'s own docstring says the codes are what
refund policy runs on — ``MENTOR_UNAVAILABLE`` refunds where
``MENTEE_NO_LONGER_NEEDED`` does not — so a mentee free to send the mentor's
code is a mentee who can claim a refund by choosing a value.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from app.domain.enums import SessionReasonCode, SessionRole, SessionStatus

__all__ = [
    "CANCELLATION_CUTOFF",
    "TRANSITIONS",
    "Transition",
    "too_late_to_cancel",
]

#: How close to the start a confirmed session stops being cancellable.
#:
#: **Not a grace period and not the join window.** Those are the two things it
#: sits between and is neither: the join window (``-5`` to ``+15`` minutes)
#: decides *attendance*, and the response window decides when an unanswered
#: *request* dies. This decides when calling off a session stops being a
#: cancellation and starts being not turning up — which is the honest record,
#: because by then the other party is already on their way to the call.
CANCELLATION_CUTOFF = dt.timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class Transition:
    """One named action: who may take it, from where, to where, and why.

    ``reasons`` is keyed by the actor's role because the permitted set differs
    by side. A blank set means the action takes no reason at all — accepting a
    request explains itself, and offering a field nobody can fill is a contract
    that invites a client to fill it.
    """

    to: SessionStatus
    #: The roles that may take this action. Two entries means either party.
    by: frozenset[SessionRole]
    #: The states it may be taken from. A single entry today for every action,
    #: and a set because the shape has to survive `no_show` and `completed`
    #: arriving with the attendance sweep.
    allowed_from: frozenset[SessionStatus]
    reasons: dict[SessionRole, frozenset[SessionReasonCode]]
    #: Whether :data:`CANCELLATION_CUTOFF` applies. Only cancelling is close
    #: enough to a start for it to bite — a *pending* request being withdrawn
    #: was never agreed, so there is nobody about to join a call.
    honours_cutoff: bool = False


#: Reasons a mentor may give. `MENTEE_NO_LONGER_NEEDED` is absent because it is
#: not the mentor's to assert, and `MENTOR_NO_SHOW` / `MENTEE_NO_SHOW` because
#: they belong to the attendance sweep — nobody is a no-show until the session
#: has run.
_MENTOR_REASONS = frozenset(
    {
        SessionReasonCode.MENTOR_UNAVAILABLE,
        SessionReasonCode.SCHEDULING_CONFLICT,
        SessionReasonCode.TECHNICAL_ISSUE,
    }
)

#: And the mentee's. `MENTOR_UNAVAILABLE` is absent for the reason the module
#: docstring gives: it is the code that refunds.
_MENTEE_REASONS = frozenset(
    {
        SessionReasonCode.MENTEE_NO_LONGER_NEEDED,
        SessionReasonCode.SCHEDULING_CONFLICT,
        SessionReasonCode.TECHNICAL_ISSUE,
    }
)

#: `EXPIRED_NO_RESPONSE`, `RESCHEDULED` and `ADMIN_ACTION` appear in no set here,
#: deliberately. The first is written by the expiry producer, which is a system
#: actor and not a person; the second needs a reschedule flow that does not
#: exist; the third belongs to an admin surface. Each is a value a party could
#: otherwise assert about themselves.

TRANSITIONS: dict[str, Transition] = {
    "accept": Transition(
        to=SessionStatus.CONFIRMED,
        by=frozenset({SessionRole.MENTOR}),
        allowed_from=frozenset({SessionStatus.PENDING_MENTOR_APPROVAL}),
        # Agreeing needs no explanation, and a reason field on it would be one
        # more thing for a client to send and for policy to have to ignore.
        reasons={},
    ),
    "decline": Transition(
        to=SessionStatus.DECLINED,
        by=frozenset({SessionRole.MENTOR}),
        allowed_from=frozenset({SessionStatus.PENDING_MENTOR_APPROVAL}),
        reasons={SessionRole.MENTOR: _MENTOR_REASONS},
    ),
    "withdraw": Transition(
        to=SessionStatus.WITHDRAWN,
        by=frozenset({SessionRole.MENTEE}),
        allowed_from=frozenset({SessionStatus.PENDING_MENTOR_APPROVAL}),
        reasons={SessionRole.MENTEE: _MENTEE_REASONS},
    ),
    "cancel": Transition(
        to=SessionStatus.CANCELLED,
        # **The only action either party may take**, and the difference from
        # `withdraw` is the state rather than the person: a confirmed session
        # was agreed, so calling it off breaks an agreement whoever does it.
        by=frozenset({SessionRole.MENTOR, SessionRole.MENTEE}),
        allowed_from=frozenset({SessionStatus.CONFIRMED}),
        reasons={SessionRole.MENTOR: _MENTOR_REASONS, SessionRole.MENTEE: _MENTEE_REASONS},
        honours_cutoff=True,
    ),
}


def too_late_to_cancel(starts_at: dt.datetime, now: dt.datetime) -> bool:
    """Whether the start is inside :data:`CANCELLATION_CUTOFF`.

    **True for a session that has already begun**, which is not incidental: the
    comparison is one-sided on purpose. A session an hour into the past is past
    cancelling, and it becomes `completed` or `no_show` through the attendance
    sweep rather than through either party changing their mind about it.

    ``now`` is passed in rather than read here, following every other
    clock-dependent function in this project: one that reads its own clock
    cannot be tested at a boundary without moving the machine's time.
    """
    return starts_at - now < CANCELLATION_CUTOFF
