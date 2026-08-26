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

from sqlalchemy import insert, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.credits import STARTER_GRANT, expiry_for
from app.domain.enums import CreditReason, CreditSource
from app.infra.db.models.credits import CreditLot, CreditTransaction

__all__ = ["grant", "grant_starter"]


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
