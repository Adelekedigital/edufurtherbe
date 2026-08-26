"""Granting credits. Every source that creates a lot comes through here.

Four things grant credits — onboarding, a qualifying invite, the monthly job and
the migration — and each writes **two rows**: the lot, and the ledger entry
saying it appeared. Four call sites each remembering to write the second is the
duplication non-negotiable #8 names, and the failure mode is the quiet one: a
balance that moved with nothing recording why, which is precisely what D8 chose
a ledger over a counter to prevent.

So the pairing lives here, once, and nothing else inserts a `credit_lots` row.

NEITHER FUNCTION COMMITS
========================
The caller does. A grant is almost never alone in its transaction — onboarding
completes *and* grants, an invite qualifies *and* grants — and a commit here
would split those into two, leaving a state where the producer fired and the
credit did not exist. The dependency that calls this is where the commit sits,
which is this project's settled shape for writes.

WHY THE STARTER HAS ITS OWN FUNCTION
====================================
It is the only grant with a once-ever guarantee, and that guarantee is a partial
unique index rather than a check. :func:`grant_starter` therefore swallows a
conflict and reports what happened; :func:`grant` cannot conflict and does not
pretend it might. Collapsing them into one function with a flag would put a
branch in the middle of the only code that creates money.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

from sqlalchemy import insert, nulls_last, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InsufficientCreditError
from app.domain.credits import STARTER_GRANT, expiry_for
from app.domain.enums import CreditReason, CreditSource
from app.infra.db.credit_store import spendable_now
from app.infra.db.models.credits import CreditLot, CreditTransaction

__all__ = ["InsufficientCreditError", "grant", "grant_starter", "spend_credit"]


async def _record(session: AsyncSession, user_id: UUID, lot_id: UUID, quantity: int) -> None:
    """The ledger entry that says a lot appeared.

    Positive delta, reason ``grant``. Written for every source without
    exception — a balance that rose with no row saying why is the state the
    ledger exists to make impossible.
    """
    await session.execute(
        insert(CreditTransaction).values(
            user_id=user_id,
            credit_lot_id=lot_id,
            delta=quantity,
            reason=CreditReason.GRANT,
            session_id=None,
        )
    )


async def grant(
    session: AsyncSession,
    user_id: UUID,
    *,
    source: CreditSource,
    quantity: int,
    now: dt.datetime,
) -> UUID:
    """Create a lot of ``source`` and record it. Returns the lot id.

    Expiry is not a parameter, deliberately: it comes from
    :func:`app.domain.credits.expiry_for`, so the four producers cannot drift on
    when a credit dies. ``REFUND`` is refused there rather than answered, since a
    refund inherits from the lot it replaces — PR 7 uses its own path.
    """
    expires_at = expiry_for(source, now=now)

    lot_id = (
        await session.execute(
            insert(CreditLot)
            .values(
                user_id=user_id,
                source=source,
                quantity_granted=quantity,
                quantity_remaining=quantity,
                expires_at=expires_at,
            )
            .returning(CreditLot.id)
        )
    ).scalar_one()

    await _record(session, user_id, lot_id, quantity)
    return UUID(str(lot_id))


async def grant_starter(session: AsyncSession, user_id: UUID) -> UUID | None:
    """The onboarding credit. ``None`` when the user already had one.

    **Idempotent by construction, not by checking.** The guarantee is
    ``uq_credit_lots_one_starter_per_user``, a partial unique index, and this
    inserts against it with ``ON CONFLICT DO NOTHING`` rather than reading first.

    A read-then-write would be a check-time-of-use race — two concurrent
    completions both see no starter, both insert, and one raises where the point
    was that neither should. Letting the database arbitrate means the loser
    learns it lost, which is what the ``None`` says.

    ``now`` is not a parameter because the starter never expires; there is no
    moment for it to be relative to. That is :func:`expiry_for`'s answer for
    ``PROFILE_COMPLETED`` and it is asserted there, so this passes ``None``
    rather than restating the rule.
    """
    lot_id = (
        await session.execute(
            pg_insert(CreditLot)
            .values(
                user_id=user_id,
                source=CreditSource.PROFILE_COMPLETED,
                quantity_granted=STARTER_GRANT,
                quantity_remaining=STARTER_GRANT,
                expires_at=None,
            )
            .on_conflict_do_nothing(
                # The partial index, named by its columns and its predicate.
                # Inferring it this way rather than by constraint name means a
                # *different* unique violation still raises, which is what keeps
                # this from swallowing a defect it was not written for.
                index_elements=["user_id"],
                index_where=text(f"source = '{CreditSource.PROFILE_COMPLETED.value}'"),
            )
            .returning(CreditLot.id)
        )
    ).scalar_one_or_none()

    if lot_id is None:
        return None

    await _record(session, user_id, UUID(str(lot_id)), STARTER_GRANT)
    return UUID(str(lot_id))


#: Namespace for every advisory lock this module takes, so a credit lock can
#: never collide with one some other subsystem takes on the same user id.
#: ``pg_advisory_xact_lock`` has a global keyspace — two callers hashing the same
#: uuid into it would serialise against each other for no reason, or worse,
#: believe they held a lock on the thing they meant.
#:
#: ``'CRED'`` read as four ASCII bytes: ``0x43524544``. Arbitrary, and that is
#: fine; what matters is that it is *stated* rather than left as a bare literal
#: somebody later reuses — and that the comment and the number agree, which they
#: did not until code review caught it. The old value, ``1_128_616_772``, is
#: ``0x43455344`` = ``CESD``.
#:
#: Changing it is safe precisely because nothing has shipped: an advisory lock
#: holds only for the life of a transaction, so there is no persisted state
#: keyed on the old number.
CREDIT_LOCK_NAMESPACE = 0x43524544


async def spend_credit(
    session: AsyncSession, user_id: UUID, session_id: UUID, *, now: dt.datetime
) -> UUID:
    """Take one credit for a booking. Returns the lot it came from.

    Raises :class:`InsufficientCreditError` when there is nothing spendable.
    Does not commit — the caller owns the transaction, and here that caller is
    the one writing the session, which is the whole point: a session that exists
    without its debit is a free booking, and a debit without its session is a
    credit taken for nothing.

    **The advisory lock is taken before the balance is read, and that ordering
    is the entire guarantee.** Reading then writing is a check-time-of-use race:
    two bookings arriving together both see a balance of one, both pass the
    check, and both insert. ``pg_advisory_xact_lock`` is held until the
    transaction ends, so the second booking blocks until the first has committed
    its debit and then reads the balance the first one left behind.

    **A wrong lock is invisible** — it does not error, it simply fails to
    protect — which is why the test for this runs two genuinely concurrent
    bookings and asserts the *rows*, rather than asserting that the lock was
    called.

    ``quantity_remaining >= 0`` is the second wall. If the lock were ever wrong,
    the database refuses the negative rather than letting a balance go under.

    **Which lot pays: soonest-expiring first, never-expiring last.** Burning a
    perishable credit before a permanent one is what a user would choose if
    asked, and it minimises the balance that quietly dies at the reset. Once
    payments land it also means free credits are spent before purchased ones,
    which is the difference between a refund conversation and a chargeback.
    ``NULLS LAST`` is PostgreSQL's default for ``ASC`` and is written anyway,
    because the whole ordering turns on it.
    """
    # `hashtextextended` is stable across sessions and returns bigint; the
    # two-argument form of the lock takes two int4s, so `hashtext` is the right
    # width. Collisions across *different* users are possible in principle and
    # harmless in practice: the cost is one booking briefly waiting for an
    # unrelated one, never a lost update.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:ns, hashtext(:key))"),
        {"ns": CREDIT_LOCK_NAMESPACE, "key": str(user_id)},
    )

    lot_id = await session.scalar(
        select(CreditLot.id)
        .where(
            CreditLot.user_id == user_id,
            CreditLot.quantity_remaining > 0,
            # The one copy of this rule — see `credit_store.spendable_now`.
            spendable_now(now),
        )
        # Soonest-expiring first, never-expiring last. `nulls_last` is
        # PostgreSQL's default for ASC and is written anyway, because the whole
        # ordering turns on it.
        .order_by(nulls_last(CreditLot.expires_at.asc()), CreditLot.created_at.asc())
        .limit(1)
    )

    if lot_id is None:
        raise InsufficientCreditError("you have no credits left")

    # **Guarded, and the rowcount is checked.** Unguarded, a lot that reached
    # zero between the select and here violates `quantity_remaining >= 0` — and
    # a CHECK violation aborts the transaction, so the caller gets a 500 on a
    # dead transaction rather than the 409 that says "you are out of credits".
    #
    # The advisory lock above should make that unreachable. This is what happens
    # when it is not: the same refusal, by the same name, rather than a 500 that
    # tells the client nothing it can act on. A test asserting only "the balance
    # never went negative" passes on the 500, which is why that is not the
    # assertion made of it.
    spent = await session.scalar(
        text(
            "UPDATE credit_lots SET quantity_remaining = quantity_remaining - 1 "
            "WHERE id = :lot AND quantity_remaining > 0 "
            "RETURNING id"
        ),
        {"lot": lot_id},
    )
    if spent is None:
        raise InsufficientCreditError("you have no credits left")

    await session.execute(
        insert(CreditTransaction).values(
            user_id=user_id,
            credit_lot_id=lot_id,
            delta=-1,
            reason=CreditReason.SESSION_BOOKED,
            session_id=session_id,
        )
    )

    return UUID(str(lot_id))
