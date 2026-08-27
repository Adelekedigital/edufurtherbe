"""The monthly grant: three credits, on the 1st, to every unlocked mentee.

**The rung that makes the ladder recur.** Without it a balance only ever
decreases — earn a starter, earn an unlock, spend both, and there is no way
back. Everything else in the credit model is a one-off.

WHO GETS IT, AND WHY EACH HALF OF THE PREDICATE IS THERE
========================================================
**A mentee goal**, because credits buy sessions and somebody who is not a mentee
books none. Written as *has a goal* rather than *is not a mentor*, which is
settled decision 15: authorization here is profile existence, so a dual-role
user is both, and the negative form would silently stop somebody booking who
can book.

**An unlock**, because that is what a qualifying invite opens. Without it the
referral programme rewards nothing — the grant would arrive whether or not
anybody ever invited a soul, and `referral_unlocks` would be a table nothing
reads.

**Alive.** The ledger keeps a deleted user's rows because it is evidence; the
grant does not follow them out.

RUNNING TWICE IS THE CASE THIS IS BUILT AROUND
==============================================
A scheduled job runs twice — a retry, a manual trigger beside the cron, an
operator checking it works. Granting again would hand every unlocked mentee six
credits, and **nothing downstream would notice**: the balance is a ``SUM`` and
it would simply be right about the wrong number.

The guard is ``uq_credit_lots_one_monthly_grant_per_period``, a partial unique
index on ``(user_id, expires_at)``. Every monthly lot granted in one month
shares an expiry — the 1st of the next month — so that pair *is* "once this
month", without the expression-immutability problem ``date_trunc`` on a
``timestamptz`` would bring. It also means a job that fires late, on the 3rd,
still collides with the one that fired on the 1st: the guard keys on the period,
not on the day the job happened to run.

``ON CONFLICT DO NOTHING`` rather than a read-then-write, for the reason every
other grant here gives: two runs starting together both see nothing and both
insert, and the database is the only thing positioned to arbitrate.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import exists, func, insert, literal, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.credits import credit_ladder, expiry_for
from app.domain.enums import CreditReason, CreditSource
from app.domain.notifications import Notification
from app.infra.db.models.credits import CreditLot, CreditTransaction
from app.infra.db.models.mentoring import MenteeGoal
from app.infra.db.models.referrals import ReferralUnlock
from app.infra.db.models.user import User
from app.infra.db.outbox import enqueue
from app.infra.db.predicates import LIVE

__all__ = ["grant_monthly_credits"]


async def grant_monthly_credits(session: AsyncSession, *, now: dt.datetime) -> int:
    """Grant this month's credits. Returns how many users were paid.

    Does not commit — the caller owns the transaction, which is what lets the
    script offer a dry run that rolls back and reports on the same query the
    real run uses.

    **One statement, not a loop.** ~1,200 mentees is one `INSERT … SELECT`
    rather than 1,200 round trips, and more importantly it is one snapshot: a
    loop reading eligibility per user would let a mentee who unlocked mid-run be
    granted or skipped depending on where the cursor was.
    """
    monthly = credit_ladder().monthly
    expires_at = expiry_for(CreditSource.MONTHLY_FREE, now=now)

    eligible = select(
        User.id,
        literal(CreditSource.MONTHLY_FREE.value).label("source"),
        literal(monthly).label("quantity_granted"),
        literal(monthly).label("quantity_remaining"),
        literal(expires_at).label("expires_at"),
    ).where(
        LIVE,
        # Credits buy sessions; somebody who is not a mentee books none.
        exists(select(MenteeGoal.id).where(MenteeGoal.user_id == User.id)),
        # The gate a qualifying invite opens.
        exists(select(ReferralUnlock.id).where(ReferralUnlock.user_id == User.id)),
    )

    granted = (
        (
            await session.execute(
                pg_insert(CreditLot)
                .from_select(
                    ["user_id", "source", "quantity_granted", "quantity_remaining", "expires_at"],
                    eligible,
                )
                # The period guard — see the module docstring.
                .on_conflict_do_nothing(
                    index_elements=["user_id", "expires_at"],
                    index_where=text(f"source = '{CreditSource.MONTHLY_FREE.value}'"),
                )
                .returning(CreditLot.id, CreditLot.user_id)
            )
        )
        .mappings()
        .all()
    )

    if not granted:
        return 0

    # **The ledger rows, from the lots that were actually created.** Derived from
    # `RETURNING` rather than from the eligible set: the two differ by exactly
    # the users the conflict clause skipped, and writing a row for a lot that
    # was not inserted is a balance claiming to have moved when it did not.
    await session.execute(
        insert(CreditTransaction),
        [
            {
                "user_id": row["user_id"],
                "credit_lot_id": row["id"],
                "delta": monthly,
                "reason": CreditReason.GRANT,
                "session_id": None,
            }
            for row in granted
        ],
    )

    # **Told in the same transaction, and only the people actually paid.**
    # Derived from the same `RETURNING` as the ledger rows for the same reason:
    # the eligible set and the granted set differ by exactly whoever the period
    # guard skipped, and telling them their credits renewed when no lot was
    # created is a message about something that did not happen.
    #
    # It follows that a second run in one month sends nothing — the guard
    # refuses the lots, `granted` is empty, and the email is refused with them.
    #
    # **A call per user rather than one carrying every recipient**, because
    # `entity_id` differs per row and `enqueue` takes one. Passing the whole set
    # with a single entity would write ~1,200 rows all claiming to be about one
    # arbitrary user. `_context_for` never reads `entity_id` for a non-session
    # message, so nothing would render wrong today — which is exactly why it
    # would go unnoticed until somebody read the outbox to answer a question
    # about who was told what.
    for row in granted:
        await enqueue(
            session,
            Notification.CREDITS_RENEWED,
            entity_type="user",
            entity_id=row["user_id"],
            recipient_ids=(row["user_id"],),
        )

    return len(granted)


async def unlocked_mentee_count(session: AsyncSession) -> int:
    """How many users the grant would reach, for the script's dry run.

    Deliberately the same two `EXISTS` as above rather than a second predicate
    that could drift — a dry run reporting on a different query from the real
    run is worse than no dry run.
    """
    return int(
        await session.scalar(
            select(func.count())
            .select_from(User)
            .where(
                LIVE,
                exists(select(MenteeGoal.id).where(MenteeGoal.user_id == User.id)),
                exists(select(ReferralUnlock.id).where(ReferralUnlock.user_id == User.id)),
            )
        )
        or 0
    )
