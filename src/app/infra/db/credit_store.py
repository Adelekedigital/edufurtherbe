"""Reading a balance, and the filter that has to agree with the expiry job.

One function today. It exists as its own module rather than a method on a
session store because **every future consumer asks the same question** — the
card, the booking gate in PR 6, and the monthly grant in PR 8 all need "what is
this user's spendable balance right now", and three hand-rolled `SUM`s with
three hand-rolled expiry predicates is the duplication non-negotiable #8 names.

TWO MECHANISMS, ONE PREDICATE
=============================
``expires_at IS NULL OR expires_at > now()`` is here, and ``credit_expiry``
writes a ``lot_expired`` row for the same lots. **Both mechanisms, deliberately.**

If only the sweep decided, a night it did not run would leave dead credits
spendable — the balance would be *wrong*, and a user could book a session with a
credit that expired last week. If only the read decided, a balance would drop
with no ledger row saying why, which is the whole of D8's argument against a
counter.

**They are not two representations of the rule, though**, which is what this
docstring used to promise a test for. The sweep asks for
``not_(spendable_now(moment))`` — this expression object, negated — so the
boundary exists once and the two cannot drift apart over it. Non-negotiable #8
prefers extraction to pinned copies; this is the extraction, and the pinning
test in ``test_credit_expiry.py`` now asserts the *behaviour* rather than
guarding a second copy.

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

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.domain.credits import (
    BonusCredits,
    CreditLadder,
    HeldLot,
    MonthlyCredits,
    allowance_for,
    end_of_month,
    split_buckets,
    state_for,
)
from app.domain.enums import CreditReason, CreditSource, CreditState
from app.infra.db.models.credits import CreditLot, CreditTransaction

__all__ = [
    "CreditSummary",
    "expiring_on",
    "get_credit_summary",
    "held",
    "spendable_now",
]


def held(moment: dt.datetime) -> list[ColumnElement[bool]]:
    """A lot with credit left in it that can still be spent: what "expiring" counts."""
    return [CreditLot.quantity_remaining > 0, spendable_now(moment)]


async def expiring_on(
    session: AsyncSession, user_id: UUID, expires_at: dt.datetime, *, now: dt.datetime
) -> int:
    """How many of this user's credits still expire at ``expires_at``, right now.

    Read **at send time** for `creditCount`: the sweep counted them when it
    queued the nudge, and a booking since then spends the soonest-expiring lot
    first, so the queued number can be stale by the time the email goes.
    """
    total = await session.scalar(
        select(func.coalesce(func.sum(CreditLot.quantity_remaining), 0)).where(
            CreditLot.user_id == user_id, CreditLot.expires_at == expires_at, *held(now)
        )
    )
    return int(total or 0)


def spendable_now(moment: dt.datetime) -> ColumnElement[bool]:
    """Whether a lot can be spent at ``moment``.

    **The one copy**, and now four consumers rather than three — the card, the
    booking gate, the monthly grant, and the expiry sweep, which takes this
    negated. The booking gate was hand-rolling its own version of this clause.
    That is the second representation non-negotiable #8 names, and it is the
    kind that drifts silently: a spend disagreeing with the balance lets
    somebody book with a credit the card already stopped showing them.

    **The sweep is why the ``IS NULL`` disjunct has to come first and stay a
    disjunct.** ``NOT (expires_at IS NULL OR expires_at > moment)`` is ``FALSE``
    for a never-expiring lot, so the sweep cannot touch one. Rewritten as the
    equivalent-looking ``coalesce(expires_at, 'infinity') > moment`` it would
    still read correctly here and still behave correctly negated — but written
    as a bare ``expires_at > moment`` with the null case "handled elsewhere",
    the negation becomes ``expires_at <= moment``, which is ``NULL`` for the
    starter and therefore false *by accident* rather than by construction.

    An expression rather than a SQL string, deliberately. A string has to be
    interpolated into the statement, which is the f-string-into-SQL shape the
    security checklist names by name — and a suppression there would be one
    more rule nobody reads.
    """
    return or_(CreditLot.expires_at.is_(None), CreditLot.expires_at > moment)


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
    #: The balance split into the monthly grant and everything else (#344).
    monthly: MonthlyCredits
    bonus: BonusCredits

    @classmethod
    def of(
        cls,
        *,
        balance: int,
        next_reset_at: dt.datetime,
        ladder: CreditLadder,
        monthly: MonthlyCredits,
        bonus: BonusCredits,
    ) -> CreditSummary:
        """Derive the three published values from the one measured one."""
        return cls(
            balance=balance,
            allowance=allowance_for(balance, ladder),
            state=state_for(balance, ladder),
            next_reset_at=next_reset_at,
            monthly=monthly,
            bonus=bonus,
        )


async def get_credit_summary(
    session: AsyncSession, user_id: UUID, *, ladder: CreditLadder, now: dt.datetime | None = None
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
            spendable_now(moment),
        )
    )

    monthly, bonus = await _buckets(session, user_id, moment=moment, ladder=ladder)
    return CreditSummary.of(
        balance=int(total or 0),
        next_reset_at=end_of_month(moment),
        ladder=ladder,
        monthly=monthly,
        bonus=bonus,
    )


async def _buckets(
    session: AsyncSession, user_id: UUID, *, moment: dt.datetime, ladder: CreditLadder
) -> tuple[MonthlyCredits, BonusCredits]:
    """The balance split into monthly and bonus credits, in two queries.

    **Every lot the user has had**, spent or not, because a refund's origin is
    usually a spent lot. Which of them still count is ``held(moment)``, selected
    as a column so the split sums exactly the lots the total does.

    A refund lot is linked to the lot it replaces through the ledger: its own
    grant row names the session, and that session's ``session_booked`` debit
    names the lot that paid. The rule for what that means is
    :func:`app.domain.credits.bucket_of`.
    """
    lots = (
        await session.execute(
            select(
                CreditLot.id,
                CreditLot.source,
                CreditLot.quantity_remaining,
                CreditLot.expires_at,
                and_(*held(moment)).label("held"),
            ).where(CreditLot.user_id == user_id)
        )
    ).all()

    grant = aliased(CreditTransaction)
    debit = aliased(CreditTransaction)
    links = (
        await session.execute(
            select(grant.credit_lot_id, debit.credit_lot_id)
            .join(CreditLot, CreditLot.id == grant.credit_lot_id)
            .join(
                debit,
                and_(
                    debit.session_id == grant.session_id,
                    debit.reason == CreditReason.SESSION_BOOKED,
                ),
            )
            .where(
                grant.user_id == user_id,
                grant.delta > 0,
                CreditLot.source == CreditSource.REFUND,
            )
        )
    ).all()

    return split_buckets(
        (
            HeldLot(
                lot_id=row.id,
                source=row.source,
                remaining=row.quantity_remaining,
                expires_at=row.expires_at,
            )
            for row in lots
            if row.held
        ),
        sources={row.id: row.source for row in lots},
        replaces={row[0]: row[1] for row in links},
        ceiling=ladder.monthly,
    )
