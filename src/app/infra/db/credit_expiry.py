"""The sweep that writes down what the balance already stopped counting.

**This does not decide what is spendable.** :func:`~app.infra.db.credit_store.
spendable_now` does, on every read, and it does so without asking whether this
job ever ran — which is the whole reason the two exist separately. A night this
sweep does not fire leaves the balance *correct* and the ledger *late*; if it
were the other way round, a mentee could book a session with a credit that died
last month.

WHAT IT IS FOR, THEN
====================
D8 chose a ledger over a balance column because a counter "leaves no record".
A lot quietly dropping out of a ``SUM`` when its date passes is exactly that
counter behaviour wearing a ledger's clothes: the number falls and nothing says
why. This sweep is the row that says why.

ONE PREDICATE, NOT TWO
======================
``credit_store`` used to book the cost of two representations of the expiry rule
and promise a test to pin them. It does not need one: the sweep asks for
``not_(spendable_now(moment))`` — the same expression object, negated — so the
read and the sweep cannot disagree about the boundary because there is only one
boundary. Non-negotiable #8 prefers extraction over pinned copies, and this is
the extraction.

The negation is correct on the ``NULL`` case, which is the half worth checking:
``NOT (expires_at IS NULL OR expires_at > moment)`` is ``FALSE`` when
``expires_at`` is null, because the first disjunct is already ``TRUE``. A
never-expiring lot is never swept. Written the obvious way — ``expires_at <=
moment`` — it would be ``NULL``, which a ``WHERE`` treats as false, so it would
happen to work today and stop working the moment somebody wrapped it in another
``NOT``.

IT DOES NOT FILTER ``LIVE``, AND THE GRANT DOES
===============================================
Granting to a soft-deleted user is a favour to somebody who is not there; the
monthly grant filters them out for that reason. **Expiry is not a favour.** It
is a fact about a lot, and it is true whether or not the holder still has an
account. Skipping them would leave un-swept lots behind a restored account whose
ledger could never explain the gap — and the ledger keeps a deleted user's rows
precisely because it is evidence (ADR 0013).

WHAT IT DOES NOT DO YET
=======================
**No batching and no supporting index.** Every dead lot is locked in one
transaction and every id goes into one ``IN`` list, which PostgreSQL's
bind-parameter ceiling caps at 32767 — roughly twenty-seven months of backlog at
today's ~1,200 monthly lots. Both are correct and cheap at this size; both are
the wrong shape at ten times it, and the fix is a ``LIMIT`` with a commit per
batch plus a partial index on ``expires_at WHERE quantity_remaining > 0``.

Named here rather than left to be discovered, because the failure is not
gradual: below the ceiling it works, above it the statement raises and the
ledger simply stops catching up.

WHY IT LOCKS
============
The rows it touches are, by construction, ones no live spend can reach:
:func:`~app.infra.db.credit_writer.spend_credit` selects under
``spendable_now``, and a refund creates a fresh lot rather than returning to
the original. But "no other writer reaches this" is an argument, and the boundary
instant is where arguments like that fail — a booking that read a lot at
``23:59:59.9`` can still be holding it when this runs at ``00:00:00.1``. Without
``FOR UPDATE`` the sweep would read ``quantity_remaining = 3``, the spend would
take one, and the ledger row would claim three credits expired when two did. The
lock costs one monthly transaction and buys a ``delta`` that is true.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import ColumnElement, func, insert, not_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import CreditReason
from app.infra.db.credit_store import spendable_now
from app.infra.db.models.credits import CreditLot, CreditTransaction

__all__ = ["expirable_credit_count", "expire_credits"]


def _dead(moment: dt.datetime) -> tuple[ColumnElement[bool], ...]:
    """Lots that have expired and still hold credits. **The one predicate.**

    Two clauses, and the second is the idempotency guard rather than an
    optimisation: a lot already at zero — spent down, or swept last month —
    has nothing to expire, and a row with ``delta = 0`` is refused by
    ``ck_credit_transactions_delta_is_not_zero`` anyway. Zeroing is
    self-terminating, so this job needs **no unique index** to make a second run
    safe, where the monthly grant needed one to stop a second run paying twice.

    That is a claim about *idempotency* and not about access paths, which is a
    different question with a different answer: nothing indexes this predicate.
    ``ix_credit_lots_user_expiry`` leads on ``user_id``, which is absent here, so
    PostgreSQL scans. Correct and cheap at this size, and a partial index on
    ``expires_at WHERE quantity_remaining > 0`` is the shape to add when it is
    not — see the module docstring.

    **A naive ``moment`` is refused rather than assumed to be UTC.** asyncpg
    binds a ``timestamptz`` by calling ``astimezone``, which on a naive
    datetime resolves against the *host's* local zone — so an operator on
    UTC+1 running this by hand would shift the boundary an hour and sweep the
    wrong set, silently and with a successful exit code. ``end_of_month``
    refuses the same input for the same reason; this is the other entry point
    into the same rule, and it was the one still guessing.
    """
    if moment.tzinfo is None:
        raise ValueError("the expiry sweep needs an aware datetime; a naive one hides its offset")
    return (not_(spendable_now(moment)), CreditLot.quantity_remaining > 0)


async def expire_credits(session: AsyncSession, *, now: dt.datetime) -> int:
    """Retire every dead lot, recording what each one lost. Returns the count.

    Does not commit: the caller owns the transaction, as everywhere else here.
    The script's dry run does *not* go through this and roll back — it calls
    :func:`expirable_credit_count`, which writes nothing at all, so there is no
    transaction to undo. (`grant_monthly_credits` claims otherwise about its own
    dry run and is wrong in the same way; the sentence was copied from it.)

    **Three statements and one transaction**, not one clever statement. An
    ``UPDATE … RETURNING`` hands back the *new* value of ``quantity_remaining``,
    which is zero and says nothing; the amount that expired is the value before
    the write, so it has to be read first. ``FOR UPDATE`` is what makes reading
    it first safe — see the module docstring.
    """
    dead = (
        (
            await session.execute(
                select(CreditLot.id, CreditLot.user_id, CreditLot.quantity_remaining)
                .where(*_dead(now))
                # **Ordered because it locks.** Two sweeps taking the same rows
                # in different scan orders deadlock; one of them is then rolled
                # back by PostgreSQL and reports a failure for a job that was
                # doing nothing wrong. The workflow's concurrency group already
                # makes two runs unlikely — this makes the pair harmless.
                .order_by(CreditLot.id)
                .with_for_update()
            )
        )
        .mappings()
        .all()
    )

    if not dead:
        return 0

    await session.execute(
        update(CreditLot)
        .where(CreditLot.id.in_([row["id"] for row in dead]))
        .values(quantity_remaining=0)
    )

    # **The row that says why the balance fell.** `session_id` is null: an
    # expiry belongs to no session, and `ck_credit_transactions_session_matches_
    # reason` refuses one that claims otherwise. `delta` is negative and never
    # zero, which the lock above is what guarantees.
    await session.execute(
        insert(CreditTransaction),
        [
            {
                "user_id": row["user_id"],
                "credit_lot_id": row["id"],
                "delta": -row["quantity_remaining"],
                "reason": CreditReason.LOT_EXPIRED,
                "session_id": None,
            }
            for row in dead
        ],
    )

    return len(dead)


async def expirable_credit_count(session: AsyncSession, *, now: dt.datetime) -> int:
    """How many lots the sweep would retire, for the script's dry run.

    The same ``_dead`` the sweep uses rather than a second predicate that could
    drift — a dry run answering a different question from the real run is worse
    than no dry run, which is the note `unlocked_mentee_count` already carries.

    **Lots, not credits**, which is what the name says and what the script
    prints. An aggregate ``count()`` with no ``GROUP BY`` always returns a row,
    so there is no ``None`` to coalesce — `unlocked_mentee_count` guards against
    one anyway, and copying that would be a guard against nothing.
    """
    return int(await session.scalar(select(func.count()).select_from(CreditLot).where(*_dead(now))))
