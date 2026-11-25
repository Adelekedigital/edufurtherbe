"""The level grants: the 1st's reset, and the mid-month top-up (decision 308).

**One mechanism for both.** A mentee's month pays their level
(``domain.credits.level_allowance``); what is still owed is the level less what
the level sources already granted this period (``level_owed``). The 1st asks it
for everybody, and a rise in level — finishing onboarding, an invite qualifying,
a session settling with them present (decision 370) — asks it for the people it
touched. The difference is only which
source the lot is written under.

RUNNING TWICE IS THE CASE THIS IS BUILT AROUND
==============================================
A scheduled job runs twice — a retry, a manual trigger beside the cron, an
operator checking it works — and a completion is retried by its client.
**Owed is counted from what the period granted, never from what is held**, so a
second run pays nothing even after the first run's credits were spent; a
balance-based refill would pay them again.

**Every writer holds the users' row locks** (``user_locks``) from before it
reads the period's grants until it commits, so the 1st and a top-up for the
same mentee — or two top-ups — queue rather than both seeing nothing granted.
``uq_credit_lots_one_monthly_grant_per_period`` stays as the database's
backstop for the 1st: a second ``monthly_free`` lot in one period is refused,
and the job writes a ``level_top_up`` instead whenever one already exists.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Collection, Mapping
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.credits import (
    LEVEL_SOURCES,
    CreditLadder,
    end_of_month,
    invite_case,
    level_allowance,
    level_owed,
)
from app.domain.enums import CreditSource
from app.domain.notifications import Notification
from app.infra.db.credit_eligibility import NO_FACTS, level_candidates, level_facts
from app.infra.db.credit_writer import grant_many
from app.infra.db.models.credits import CreditLot
from app.infra.db.models.user import User
from app.infra.db.outbox import enqueue
from app.infra.db.user_locks import lock_users

__all__ = [
    "grant_monthly_credits",
    "owed_count",
    "owed_top_ups",
    "tell_paid",
    "top_up_and_tell",
    "top_up_everyone",
    "top_up_levels",
]


async def _period_grants(
    session: AsyncSession, user_ids: Collection[UUID], period_end: dt.datetime
) -> dict[UUID, tuple[int, bool]]:
    """Per user: what the level sources granted this period, and whether the
    1st's ``monthly_free`` lot is among them.

    ``quantity_granted``, not ``quantity_remaining``: what was paid, spent or
    not. A period is its expiry — every level lot dies at the month end.
    """
    rows = await session.execute(
        select(
            CreditLot.user_id,
            func.sum(CreditLot.quantity_granted),
            func.bool_or(CreditLot.source == CreditSource.MONTHLY_FREE),
        )
        .where(
            CreditLot.user_id.in_(list(user_ids)),
            CreditLot.source.in_(LEVEL_SOURCES),
            CreditLot.expires_at == period_end,
        )
        .group_by(CreditLot.user_id)
    )
    return {row[0]: (int(row[1]), bool(row[2])) for row in rows}


async def _owed(
    session: AsyncSession,
    user_ids: Collection[UUID],
    *,
    monthly: bool,
    ladder: CreditLadder,
    now: dt.datetime,
    up_to: int | None = None,
) -> dict[CreditSource, dict[UUID, int]]:
    """What each user's level still owes this period, by the source it is
    written under, omitting anybody owed nothing. Reads only.

    ``monthly`` writes the 1st's ``monthly_free`` lot, unless the period
    already has one — then the difference is a ``level_top_up``, so the
    one-per-period index is never what refuses a legitimate payment.
    """
    facts = await level_facts(session, user_ids)
    granted = await _period_grants(session, user_ids, end_of_month(now))

    owed: dict[CreditSource, dict[UUID, int]] = {}
    for user_id in user_ids:
        already, has_monthly = granted.get(user_id, (0, False))
        allowance = level_allowance(facts.get(user_id, NO_FACTS), ladder)
        if up_to is not None:
            allowance = min(allowance, up_to)
        amount = level_owed(allowance, already)
        if amount:
            source = (
                CreditSource.MONTHLY_FREE
                if monthly and not has_monthly
                else CreditSource.LEVEL_TOP_UP
            )
            owed.setdefault(source, {})[user_id] = amount
    return owed


async def _pay_levels(
    session: AsyncSession,
    user_ids: Collection[UUID],
    *,
    monthly: bool,
    ladder: CreditLadder,
    now: dt.datetime,
    up_to: int | None = None,
) -> dict[UUID, int]:
    """Grant each user what their level still owes this period. Returns what
    was paid, by user, omitting anybody owed nothing. Does not commit.

    The users' rows are locked before anything is read, and every lot is
    written in bulk (`grant_many`), so the locks are held for a few statements
    rather than a round trip per user.
    """
    if not user_ids:
        return {}
    await lock_users(session, user_ids)
    paid: dict[UUID, int] = {}
    for source, quantities in (
        await _owed(session, user_ids, monthly=monthly, ladder=ladder, now=now, up_to=up_to)
    ).items():
        await grant_many(session, quantities, source=source, now=now)
        paid.update(quantities)
    return paid


async def top_up_levels(
    session: AsyncSession,
    user_ids: Collection[UUID],
    *,
    ladder: CreditLadder,
    now: dt.datetime,
    up_to: int | None = None,
) -> dict[UUID, int]:
    """Top this period up to each user's level, after something raised it.
    Returns what was paid, by user. Does not commit.

    Safe to call whenever a level *may* have risen: a user whose period already
    paid their level is owed nothing and gets no lot. ``up_to`` caps what this
    period may reach now (decision 358: a first goal pays the starter amount at
    once; the full level follows on the 1st).
    """
    return await _pay_levels(session, user_ids, monthly=False, ladder=ladder, now=now, up_to=up_to)


async def top_up_and_tell(
    session: AsyncSession,
    user_id: UUID,
    *,
    ladder: CreditLadder,
    now: dt.datetime,
    up_to: int | None = None,
) -> int:
    """Top one user up (`top_up_levels`) and, if anything was paid, tell them
    with `credits_granted` **in the same transaction**, so a message about a
    credit that rolled back cannot exist. Returns what was paid; does not
    commit. One statement of "pay and say so", read by onboarding completion
    and a first goal (decision 358).

    Guarded on what was paid rather than sent unconditionally: a retry pays
    nothing, and telling somebody twice that their credit arrived is worse than
    a slightly late first telling.
    """
    paid = await top_up_levels(session, [user_id], ladder=ladder, now=now, up_to=up_to)
    # The starter email keeps its own wording: it is the first credit, and the
    # invite step is introduced by the first session's (decision 371).
    await tell_paid(session, paid, Notification.CREDITS_GRANTED, invite_wording=False)
    return paid.get(user_id, 0)


async def tell_paid(
    session: AsyncSession,
    paid: Mapping[UUID, int],
    notification: Notification,
    *,
    invite_wording: bool = True,
) -> None:
    """Tell each user what they were just paid, in the paying transaction, so
    a message about a credit that rolled back cannot exist. Does not commit.
    **The one statement of "pay and say so"**, read by every credits email
    that follows a payment.

    With what was paid as ``credit_count`` (decision 323). With
    ``invite_wording``, anyone a qualified invite would still lift reads the
    invite case (`invite_case`, decision 371), judged on the facts after the
    payment.
    """
    if not paid:
        return
    facts = await level_facts(session, list(paid)) if invite_wording else {}
    for user_id, amount in paid.items():
        variables: dict[str, object] = {"credit_count": amount}
        if case := invite_case(facts.get(user_id, NO_FACTS)):
            variables["line_case"] = case
        await enqueue(
            session,
            notification,
            entity_type="user",
            entity_id=user_id,
            recipient_ids=(user_id,),
            variables=variables,
        )


async def grant_monthly_credits(
    session: AsyncSession, *, now: dt.datetime, ladder: CreditLadder
) -> int:
    """The 1st: pay every mentee their level. Returns how many were paid.

    Does not commit — the caller owns the transaction. One snapshot of who is
    eligible, every row locked in id order, so a mentee who levels up while the
    job runs is paid once, either here or by their own top-up after it.
    """
    paid = await _pay_levels(
        session, await _candidates(session), monthly=True, ladder=ladder, now=now
    )

    # **Told in the same transaction, and only the people actually paid**, with
    # the amount: a level-1 mentee renews one credit, not three. A second run
    # in one month pays nothing, so it sends nothing. Anyone below 3 an invite
    # would lift reads the invite wording (decision 371).
    await tell_paid(session, paid, Notification.CREDITS_RENEWED)
    return len(paid)


async def _candidates(session: AsyncSession) -> list[UUID]:
    """Everybody whose level may be above 0 (`level_candidates`): the one
    snapshot the 1st, its dry run and the one-off top-up all start from."""
    return list((await session.scalars(select(User.id).where(*level_candidates()))).all())


async def owed_top_ups(
    session: AsyncSession, *, now: dt.datetime, ladder: CreditLadder
) -> dict[UUID, int]:
    """What every candidate's level still owes this period, by user: the dry
    run of the 1st and of `top_up_everyone`. Reads only; the same arithmetic
    (`_owed`). Which source a payment is written under does not change it."""
    candidates = await _candidates(session)
    if not candidates:
        return {}
    owed = await _owed(session, candidates, monthly=False, ladder=ladder, now=now)
    return {
        user_id: amount for quantities in owed.values() for user_id, amount in quantities.items()
    }


async def top_up_everyone(
    session: AsyncSession, *, now: dt.datetime, ladder: CreditLadder
) -> dict[UUID, int]:
    """Top every candidate up to their level this period, now. Returns what was
    paid, by user. Does not commit.

    For a change to the level rule mid-month (decision 370): the 1st would pay
    the new levels anyway, and this pays them at once. Silent, as the top-up at
    a settlement is. Safe to re-run: a period already at its level owes nothing.
    """
    return await top_up_levels(session, await _candidates(session), ladder=ladder, now=now)


async def owed_count(session: AsyncSession, *, now: dt.datetime, ladder: CreditLadder) -> int:
    """How many users the 1st's grant would pay now, for the job's dry run.

    The same candidates and the same arithmetic as the real run (`_owed`), so
    a dry run after the month was paid reports 0, as the real run would pay
    nobody (found by code review: it used to count candidates). Reads only.
    """
    return len(await owed_top_ups(session, now=now, ladder=ladder))
