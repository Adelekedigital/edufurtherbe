"""An admin putting credits into somebody's balance, and the record of who did.

**Support's only way to make somebody whole.** Every other credit here is
automatic — onboarding, a qualifying invite, the monthly job, a refund, the
migration — so before this, a mentee wrongly charged, or one whose session broke
in a way no refund path covers, could only be fixed with direct SQL against
production. For something that behaves like money that is the wrong tool.

BULK, BECAUSE THE REAL CASE IS BULK
===================================
An outage costs a cohort, not a person. Granting one at a time would mean an
admin issuing forty requests and having no way to know which of them landed —
so this takes a list, writes them in one transaction, and reports what it could
not resolve rather than failing the lot of them.

**Partial resolution is a result, not an error.** An admin correcting six people
should not lose five because one id was stale. The unresolved ids come back by
name, which is the only form somebody can act on.

IT EXPIRES LIKE EVERY OTHER GRANT
=================================
`expiry_for` decides, and `ADMIN_GRANT` is not in `NON_EXPIRING` — only the
starter is, and that is because it exists to give somebody a first taste of the
platform rather than because grants are permanent. So an admin credit dies at
the end of the month like the monthly grant does, and the expiry sweep writes
its `lot_expired` row without knowing this source exists.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationError
from app.domain.credits import credit_ladder, expiry_for
from app.domain.enums import CreditReason, CreditSource
from app.infra.db.models.credits import AdminCreditGrant, CreditLot, CreditTransaction
from app.infra.db.models.user import User
from app.infra.db.predicates import LIVE

__all__ = ["AdminGrantResult", "grant_credits"]


@dataclass(frozen=True, slots=True)
class AdminGrantResult:
    """What landed, and who could not be found.

    **Both halves, because a count alone is unactionable.** An admin who asked
    for six and got five needs the sixth id to go and look at it; a number tells
    them only that something went wrong.
    """

    granted: tuple[UUID, ...]
    unresolved: tuple[UUID, ...]

    @property
    def quantity_granted(self) -> int:
        return len(self.granted)


async def grant_credits(
    session: AsyncSession,
    *,
    admin_id: UUID,
    user_ids: tuple[UUID, ...],
    quantity: int,
    note: str | None,
    now: dt.datetime,
) -> AdminGrantResult:
    """Give ``quantity`` credits to each live user named. Does not commit.

    **The cap is the monthly grant**, read from the configured ladder rather
    than written here — so raising the allowance raises what an admin may hand
    out in one action, and the two cannot drift. Refused rather than clamped: an
    admin who typed 500 meant something, and quietly granting three would leave
    them believing the larger number went in.

    **Live users only.** A soft-deleted account is not somebody to credit, and
    the lot's foreign key would accept them — `LIVE` is the only thing that
    would not.
    """
    if now.tzinfo is None:
        raise ValueError("an admin grant needs an aware datetime; a naive one hides its offset")

    ceiling = credit_ladder().monthly
    if quantity > ceiling:
        raise ValidationError(
            f"an admin grant is capped at {ceiling} credits; asked for {quantity}"
        )

    if not user_ids:
        return AdminGrantResult(granted=(), unresolved=())

    # **Resolved against `users`, not assumed.** An id that names nobody — or a
    # soft-deleted account — writes nothing and comes back as unresolved, where
    # inserting blind would raise on the foreign key and lose the whole batch.
    found = set((await session.scalars(select(User.id).where(User.id.in_(user_ids), LIVE))).all())
    # Ordered by the caller's list rather than by the set, so the answer reads
    # in the order they asked — and so two identical requests produce identical
    # responses, which is what makes the idempotent replay comparable.
    resolved = tuple(user_id for user_id in dict.fromkeys(user_ids) if user_id in found)
    unresolved = tuple(user_id for user_id in dict.fromkeys(user_ids) if user_id not in found)

    if not resolved:
        return AdminGrantResult(granted=(), unresolved=unresolved)

    expires_at = expiry_for(CreditSource.ADMIN_GRANT, now=now)

    lots = (
        (
            await session.execute(
                insert(CreditLot).returning(CreditLot.id, CreditLot.user_id),
                [
                    {
                        "user_id": user_id,
                        "source": CreditSource.ADMIN_GRANT,
                        "quantity_granted": quantity,
                        "quantity_remaining": quantity,
                        "expires_at": expires_at,
                    }
                    for user_id in resolved
                ],
            )
        )
        .mappings()
        .all()
    )

    # **The ledger row, derived from the lots that exist.** Same care
    # `grant_monthly_credits` takes: a movement written for a lot that was not
    # inserted is a balance claiming to have moved when it did not.
    await session.execute(
        insert(CreditTransaction),
        [
            {
                "user_id": lot["user_id"],
                "credit_lot_id": lot["id"],
                "delta": quantity,
                "reason": CreditReason.GRANT,
                "session_id": None,
            }
            for lot in lots
        ],
    )

    # **And the record of who authorised it**, in the same transaction. An
    # `admin_grant` lot with no row here is credits nobody can be asked about,
    # which is the state this table exists to make impossible.
    await session.execute(
        insert(AdminCreditGrant),
        [
            {
                "credit_lot_id": lot["id"],
                "user_id": lot["user_id"],
                "granted_by": admin_id,
                "note": note,
            }
            for lot in lots
        ],
    )

    return AdminGrantResult(granted=tuple(lot["user_id"] for lot in lots), unresolved=unresolved)
