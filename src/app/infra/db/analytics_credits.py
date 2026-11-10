"""The admin Analytics Credits tab (PRD 31, decision 324): what happened to
credits in a window, in total and by where they came from.

Read off the ledger, `credit_transactions`, by the row's own time:

* **granted**: `grant` rows. A refund lot's credit is written under its refund
  reason, never `grant`, so the `refund` group's granted is its refunds.
* **spent**: `session_booked` debits. **expired**: `lot_expired` debits.
* **refunded**: the refund reasons (`REASON_KINDS` says which), credited to the
  group that **paid** for the session: the `session_booked` debit of its paying
  session (`credit_store.paying_sessions`, so a moved session's refund goes to
  the source its first session spent).

`flow.granted` leaves refunds out (the PRD's "Monthly, starter, referral,
admin"); `flow.refunded` is every refund. Everyone `analytics_scope` leaves out
is left out.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.domain.analytics import (
    CREDIT_GROUPS,
    SOURCE_GROUP,
    CreditFigures,
    CreditGroup,
    CreditsTab,
)
from app.domain.credit_log import REASON_KINDS
from app.domain.enums import CreditReason, CreditSource
from app.domain.mentor_stats import Window
from app.infra.db.analytics_scope import counted_user
from app.infra.db.credit_store import paying_sessions
from app.infra.db.models.credits import CreditLot, CreditTransaction
from app.infra.db.predicates import during

#: The refund reasons, read from the credit log's one table of kinds.
REFUND_REASONS = tuple(reason for reason, kind in REASON_KINDS.items() if kind == "refund")


async def credits_tab(session: AsyncSession, window: Window) -> CreditsTab:
    """The tab's figures for ``window``."""
    groups: dict[CreditGroup, CreditFigures] = {g: CreditFigures() for g in CREDIT_GROUPS}
    flow = CreditFigures()

    moves = await session.execute(
        select(CreditLot.source, CreditTransaction.reason, func.sum(CreditTransaction.delta))
        .join(CreditLot, CreditLot.id == CreditTransaction.credit_lot_id)
        .where(
            during(CreditTransaction.created_at, window), counted_user(CreditTransaction.user_id)
        )
        .group_by(CreditLot.source, CreditTransaction.reason)
    )
    for source, reason, total in moves.all():
        group = groups[SOURCE_GROUP[CreditSource(source)]]
        amount = abs(int(total or 0))
        if reason == CreditReason.GRANT:
            group.granted += amount
            flow.granted += amount
        elif reason == CreditReason.SESSION_BOOKED:
            group.spent += amount
            flow.spent += amount
        elif reason == CreditReason.LOT_EXPIRED:
            group.expired += amount
            flow.expired += amount
        elif reason in REFUND_REASONS:
            group.granted += amount
            flow.refunded += amount

    for source, total in (await session.execute(_refunds_by_paying_source(window))).all():
        groups[SOURCE_GROUP[CreditSource(source)]].refunded += int(total or 0)
    return CreditsTab(flow=flow, by_source=groups)


def _refunds_by_paying_source(window: Window) -> Any:
    """Refunds in ``window``, summed by the source of the lot that paid."""
    refund = aliased(CreditTransaction)
    debit = aliased(CreditTransaction)
    paid_from = aliased(CreditLot)
    payers = paying_sessions(None)
    return (
        select(paid_from.source, func.sum(refund.delta))
        .select_from(refund)
        .join(payers, payers.c.session_id == refund.session_id)
        .join(
            debit,
            and_(
                debit.session_id == payers.c.payer_id,
                debit.reason == CreditReason.SESSION_BOOKED,
            ),
        )
        .join(paid_from, paid_from.id == debit.credit_lot_id)
        .where(
            refund.reason.in_(REFUND_REASONS),
            during(refund.created_at, window),
            counted_user(refund.user_id),
        )
        .group_by(paid_from.source)
    )
