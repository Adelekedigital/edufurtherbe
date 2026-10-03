"""When a booked credit comes back, and under which reason.

Pure, and the one place the rule lives. The transitions, the expiry sweep and
the attendance settlement each ask here and write what they are told, so a
change of policy is a change of :class:`RefundPolicy` rather than of three
writers that would have to agree.

**The owner's decision, 2026-10-03** (settled decision 229):

* A request that never became a session — declined, withdrawn, expired — always
  refunds. Nothing was agreed, so nothing was owed.
* A **mentor** cancelling a confirmed session always refunds the mentee,
  whatever reason they give or none. The mentee did nothing to lose it.
* A **mentee** cancelling refunds only with at least
  :attr:`RefundPolicy.mentee_cancel_notice` to go. Inside it the credit is
  used: the mentor kept the hour and it is too late to fill it.
* A **mentor no-show** — the mentee arrived and the mentor never did — refunds
  the mentee. A mentee no-show refunds nothing, and neither does a session both
  missed, which no single party can be blamed for.

**Parameters, not constants in a handler** (the open question this closed asked
for exactly that), so moving the window is one value. The ten-minute
:data:`app.domain.sessions.CANCELLATION_CUTOFF` is a different rule — it decides
whether cancelling is allowed at all — and is left where it is.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from app.domain.attendance import absent_party
from app.domain.enums import CreditReason, SessionRole, SessionStatus

__all__ = [
    "REFUND_POLICY",
    "RefundPolicy",
    "never_agreed_refund",
    "no_show_refund",
    "transition_refund",
]


@dataclass(frozen=True, slots=True)
class RefundPolicy:
    #: How much notice a mentee's cancellation needs to get the credit back.
    #: Exactly this much refunds: the boundary belongs to the mentee.
    mentee_cancel_notice: dt.timedelta = dt.timedelta(hours=12)


REFUND_POLICY = RefundPolicy()

#: Terminal statuses for a request that never became a session.
_NEVER_AGREED = frozenset({SessionStatus.DECLINED, SessionStatus.WITHDRAWN, SessionStatus.EXPIRED})


def never_agreed_refund(to: SessionStatus) -> CreditReason | None:
    """The refund for a request that ended without becoming a session: always,
    whoever ended it and whenever. Needs no clock, which is why the expiry sweep
    asks this rather than :func:`transition_refund`."""
    return CreditReason.REQUEST_UNFULFILLED if to in _NEVER_AGREED else None


def transition_refund(
    to: SessionStatus,
    *,
    actor: SessionRole | None,
    starts_at: dt.datetime,
    now: dt.datetime,
    policy: RefundPolicy = REFUND_POLICY,
) -> CreditReason | None:
    """The refund a move to ``to`` owes the mentee, or ``None``.

    ``actor`` is who moved it, and ``None`` for the system (the expiry sweep).
    """
    if (owed := never_agreed_refund(to)) is not None:
        return owed
    if to is not SessionStatus.CANCELLED:
        return None
    if actor is SessionRole.MENTOR:
        return CreditReason.SESSION_CANCELLED_REFUND
    if actor is SessionRole.MENTEE and starts_at - now >= policy.mentee_cancel_notice:
        return CreditReason.SESSION_CANCELLED_REFUND
    return None


def no_show_refund(*, mentor_came: bool, mentee_came: bool) -> CreditReason | None:
    """The refund a settled session owes the mentee: only when the mentor alone
    was absent, as :func:`app.domain.attendance.absent_party` decides it."""
    if absent_party(mentor_attended=mentor_came, mentee_attended=mentee_came) is SessionRole.MENTOR:
        return CreditReason.SESSION_NO_SHOW_REFUND
    return None
