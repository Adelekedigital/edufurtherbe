"""Reading a balance, and the filter that has to agree with the expiry job.

One function today. It exists as its own module rather than a method on a
session store because **every future consumer asks the same question** — the
card, the booking gate in PR 6, and the monthly grant in PR 8 all need "what is
this user's spendable balance right now", and three hand-rolled `SUM`s with
three hand-rolled expiry predicates is the duplication non-negotiable #8 names.

THE EXPIRY PREDICATE LIVES IN TWO PLACES ON PURPOSE
===================================================
``expires_at IS NULL OR expires_at > now()`` is here, and the expiry job in
PR 9 writes a ``lot_expired`` row for the same lots. **Both, deliberately.**

If only the job decided, a night it did not run would leave dead credits
spendable — the balance would be *wrong*, and a user could book a session with a
credit that expired last week. If only the read decided, a balance would drop
with no ledger row saying why, which is the whole of D8's argument against a
counter.

Two representations of one rule is #8, so this is a real cost and it is paid
knowingly: the exception is that the two answer *different questions* — what is
spendable now, and what happened. PR 9 owes a test pinning them together, and
until that test exists this comment is the only thing holding them.

NULL IS NOT ZERO, AND ``SUM`` RETURNS NULL
==========================================
A user with no lots at all — every migrated mentor, and anybody before their
first grant — has ``SUM`` return ``NULL`` rather than ``0``. Coalesced here
rather than at the call site, because a ``None`` balance reaching
:func:`state_for` raises and a ``None`` reaching the card renders nothing.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.credits import allowance_for, end_of_month, state_for
from app.domain.enums import CreditState
from app.infra.db.models.credits import CreditLot

__all__ = ["CreditSummary", "get_credit_summary"]


@dataclass(frozen=True, slots=True)
class CreditSummary:
    """What the dashboard card needs, and nothing else.

    Not a Pydantic model: this is ``infra`` handing ``api`` a plain object, and
    the response schema is where the wire shape is declared. Not the ORM rows
    either — the card never needs a lot.

    A frozen dataclass, matching `AuthUser` and `ProfileEvidence` rather than a
    hand-rolled ``__slots__`` class. Frozen because nothing downstream should be
    adjusting a balance on its way to the wire.
    """

    balance: int
    allowance: int
    state: CreditState
    next_reset_at: dt.datetime

    @classmethod
    def of(cls, *, balance: int, next_reset_at: dt.datetime) -> CreditSummary:
        """Derive the three published values from the one measured one."""
        return cls(
            balance=balance,
            allowance=allowance_for(balance),
            state=state_for(balance),
            next_reset_at=next_reset_at,
        )


async def get_credit_summary(
    session: AsyncSession, user_id: UUID, *, now: dt.datetime | None = None
) -> CreditSummary:
    """This user's spendable balance, banded, with the date it resets.

    ``now`` is injectable and **no caller passes it**, which is deliberate
    rather than an oversight. The expiry boundary is the thing under test here
    and it moves with the calendar: without an injection point a test has to
    write `expires_at` relative to the real clock, and a suite that does that
    passes in the first week of a month and fails in the last. Production takes
    the default, which is the only correct value there.
    """
    moment = now or dt.datetime.now(dt.UTC)

    total = await session.scalar(
        select(func.coalesce(func.sum(CreditLot.quantity_remaining), 0)).where(
            CreditLot.user_id == user_id,
            # See the module docstring. The job does not decide what is
            # spendable; this does.
            or_(CreditLot.expires_at.is_(None), CreditLot.expires_at > moment),
        )
    )

    return CreditSummary.of(balance=int(total or 0), next_reset_at=end_of_month(moment))
