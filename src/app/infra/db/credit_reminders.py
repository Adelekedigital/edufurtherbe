"""Nudging somebody who is sitting on credits that are about to expire.

**The one credit message about something that has not happened yet.** Every
other one reports a fact — a grant landed, a month turned, a lot expired. This
exists to change what somebody does before the month ends, which is also why it
has to be right about who is nudged: a mentee told their credits are running out
when nothing of theirs is expiring learns to ignore the sender.

WHO IT REACHES
==============
Somebody holding a *spendable* lot whose expiry falls inside a bounded window
around the offset:

    quantity_remaining > 0                they have something to lose
    spendable_now(moment)                 an already-dead lot is not "about to"
    target - WINDOW <= expires_at < target + WINDOW

**The starter is excluded by the window being bounded on both sides**, and that
is worth stating because the obvious defence against it — an explicit
``expires_at IS NOT NULL`` — turns out to be unconditionally redundant. A lot
that never expires has no date to fall between two others; not by `NULL`
comparison semantics, which a rewrite could undo, but because there is no value
to place. Both rewrites were tried: widening the range with
``coalesce(expires_at, 'infinity')`` still excludes it, since a sentinel far
future is outside any window an offset produces.

That matters. A user whose only credit is the never-expiring starter has nothing
running out, and a filter written as "has any balance" would warn them every
fortnight for the life of the account.

**Mentees, implicitly.** No role predicate: a mentor holds no credits, so the
balance condition already excludes them. Adding one would be settled decision 15's
mistake in the other direction — a dual-role user does hold credits and does
book.

THE PERIOD IS IN THE DEDUP KEY
==============================
`uq_outbox_events_reminder` is unique on ``(entity_id, event_type, kind,
recipient)``. `entity_id` here is the user, so a bare ``c14`` would be unique
across their entire lifetime and they would be nudged **once, ever**. Every other
reminder in this codebase hangs off a session and fires once by nature;
:func:`~app.domain.notifications.credit_reminder_kind` is what makes a recurring
one work, and it is the reason that function exists rather than a bare constant.

RUN IT DAILY, AND LET THE INDEX DEDUPLICATE
===========================================
The sweep asks *which reminders are due now*, so it is safe to run at any
cadence: a day missed is a nudge not sent, never a nudge sent twice. The window
is a day wide on each side of the offset, so a run that slips by hours still
finds the same people — and a run that repeats within the window writes nothing,
because the index refuses the second row.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.notifications import CREDIT_REMINDERS, CreditReminder, Notification
from app.domain.notifications import credit_reminder_kind as kind_for
from app.infra.db.credit_store import spendable_now
from app.infra.db.models.credits import CreditLot
from app.infra.db.models.user import User
from app.infra.db.outbox import enqueue
from app.infra.db.predicates import LIVE

__all__ = ["CREDIT_REMINDERS", "WINDOW", "expiring_soon", "remind_about_expiring_credits"]

#: How wide a net each offset casts. **A day**, so a job that fires late still
#: finds the same people — the alternative is an exact-moment match that misses
#: everybody whenever a scheduled run slips, which GitHub's own documentation
#: says it will.
WINDOW = dt.timedelta(days=1)


async def expiring_soon(
    session: AsyncSession, reminder: CreditReminder, *, now: dt.datetime
) -> list[tuple[UUID, dt.datetime]]:
    """Users with spendable credits expiring around ``reminder.before`` from now.

    Returns one row per user per expiry date, because a user can hold two lots
    with different expiries and each is its own thing to be told about — the
    dedup key carries the period for exactly that reason.
    """
    if now.tzinfo is None:
        raise ValueError("the credit reminder sweep needs an aware datetime")

    target = now + reminder.before

    return [
        (row.user_id, row.expires_at)
        for row in await session.execute(
            select(CreditLot.user_id, CreditLot.expires_at)
            .join(User, User.id == CreditLot.user_id)
            .where(
                LIVE,
                CreditLot.quantity_remaining > 0,
                # `spendable_now` admits the never-expiring starter, deliberately
                # — it *is* spendable. What excludes it here is the two-sided
                # window below: a lot with no expiry has no date to fall in one.
                # See the module docstring for why an explicit `IS NOT NULL`
                # would add nothing.
                spendable_now(now),
                CreditLot.expires_at >= target - WINDOW,
                CreditLot.expires_at < target + WINDOW,
            )
            .group_by(CreditLot.user_id, CreditLot.expires_at)
            # A user with three monthly lots sharing one expiry is one person
            # with one deadline, not three nudges.
            .having(func.sum(CreditLot.quantity_remaining) > 0)
        )
    ]


async def remind_about_expiring_credits(session: AsyncSession, *, now: dt.datetime) -> int:
    """Queue every nudge that is due. Returns how many were written. No commit.

    **Every offset on every run**, rather than working out which one "today" is.
    The sweep asks what is due and the index refuses what has already gone, so
    there is no calendar arithmetic here to get wrong and no state to keep about
    which reminders have fired.
    """
    queued = 0
    for reminder in CREDIT_REMINDERS:
        for user_id, expires_at in await expiring_soon(session, reminder, now=now):
            await enqueue(
                session,
                Notification.CREDITS_EXPIRING,
                entity_type="user",
                entity_id=user_id,
                recipient_ids=(user_id,),
                variables={
                    "kind": kind_for(reminder, expires_at),
                    # The words travel with the schedule, so changing an offset
                    # moves the wording with it rather than leaving a message
                    # that says "two weeks" one week out.
                    "interval": reminder.interval,
                },
            )
            queued += 1
    return queued
