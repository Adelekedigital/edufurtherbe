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
from typing import Any
from uuid import UUID

from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationError
from app.domain.credits import CreditLadder, expiry_for
from app.domain.enums import CreditReason, CreditSource
from app.infra.db.models.credits import AdminCreditGrant, CreditLot, CreditTransaction
from app.infra.db.models.user import User
from app.infra.db.predicates import LIVE

__all__ = ["AdminGrantResult", "grant_credits", "list_admin_grants"]


@dataclass(frozen=True, slots=True)
class AdminGrantResult:
    """What landed, and who could not be found.

    **Both halves, because a count alone is unactionable.** An admin who asked
    for six and got five needs the sixth id to go and look at it; a number tells
    them only that something went wrong.
    """

    granted: tuple[UUID, ...]
    unresolved: tuple[UUID, ...]


async def grant_credits(
    session: AsyncSession,
    *,
    admin_id: UUID,
    user_ids: tuple[UUID, ...],
    quantity: int,
    note: str | None,
    now: dt.datetime,
    ladder: CreditLadder,
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

    if quantity < 1:
        # The schema's `ge=1` stops this from the API, but the docstring above
        # positions this function as the rule's home — and a script calling it
        # with zero would otherwise get
        # `ck_credit_lots_quantity_granted_positive`, a database error naming a
        # constraint rather than the argument.
        raise ValidationError(f"an admin grant must be at least 1 credit; asked for {quantity}")

    ceiling = ladder.monthly
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
    # in the order they asked. **Not for the replay** — an earlier comment here
    # claimed that, and it was wrong: a replay returns the body stored in
    # `idempotency_keys` verbatim, so ordering plays no part in it.
    resolved = tuple(user_id for user_id in dict.fromkeys(user_ids) if user_id in found)
    unresolved = tuple(user_id for user_id in dict.fromkeys(user_ids) if user_id not in found)

    if not resolved:
        return AdminGrantResult(granted=(), unresolved=unresolved)

    expires_at = expiry_for(CreditSource.ADMIN_GRANT, now=now)

    lots = (
        (
            await session.execute(
                insert(CreditLot).returning(
                    CreditLot.id,
                    CreditLot.user_id,
                    # **Not decoration.** SQLAlchemy's `insertmanyvalues` does
                    # not guarantee RETURNING rows correspond to input order
                    # without this, and the response lists recipients from
                    # these rows — so an admin could be told they credited a
                    # different set from the one they asked for.
                    sort_by_parameter_order=True,
                ),
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


#: **Both names, and the grant's own outcome.** An audit asks three things of a
#: row: who was credited, who did it, and whether the credits are still there.
#: The last is why `quantity_remaining` and `expires_at` are joined in rather
#: than only the amount granted — "we gave them three" and "they still have
#: three" are different facts, and a history that shows only the first cannot
#: answer whether a goodwill gesture ever reached anybody.
LISTED_GRANTS = """
SELECT a.id,
       a.created_at,
       a.note,
       a.user_id,
       recipient.first_name AS recipient_first_name,
       recipient.last_name  AS recipient_last_name,
       a.granted_by,
       admin.first_name     AS granted_by_first_name,
       admin.last_name      AS granted_by_last_name,
       l.quantity_granted,
       l.quantity_remaining,
       l.expires_at
FROM admin_credit_grants a
JOIN credit_lots l ON l.id = a.credit_lot_id
JOIN users recipient ON recipient.id = a.user_id
JOIN users admin ON admin.id = a.granted_by
"""


async def list_admin_grants(
    session: AsyncSession,
    *,
    limit: int,
    after: tuple[str, UUID] | None = None,
    granted_by: UUID | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """One page of admin credit grants, newest first.

    **Inner joins on both users, deliberately.** `granted_by` and `user_id` are
    both `RESTRICT`, so neither party can be hard-deleted while a row exists —
    an outer join would be guarding against a state the schema forbids, and
    would read as though the history could name somebody who is gone.

    **Soft-deleted recipients are shown.** A credit granted to somebody who has
    since closed their account is exactly the kind of thing an audit is for, and
    filtering `LIVE` here would quietly shorten the history rather than
    answering the question asked of it.

    ``granted_by`` narrows to one admin — *"what did I hand out"*, which is the
    second question this table gets asked and the one a person asks about
    themselves.
    """
    clauses = []
    params: dict[str, Any] = {"limit": limit + 1}
    if granted_by is not None:
        clauses.append("a.granted_by = :granted_by")
        params["granted_by"] = granted_by
    if after is not None:
        # Keyset on `(created_at, id)`, the shape ADR 0016 settled and every
        # other list here uses. Both halves, because `created_at` alone is not
        # unique — a bulk grant writes every row in one transaction and they
        # share a timestamp to the microsecond.
        clauses.append("(a.created_at, a.id) < (:after_at, :after_id)")
        # **Parsed, not bound raw.** The cursor carries the timestamp as an ISO
        # string and asyncpg binds `timestamptz` from a `datetime` only — passing
        # the string through raises `DataError` on page *two*, which is the shape
        # of paging bug `mentor_reviews_page` records having shipped once.
        try:
            params["after_at"] = dt.datetime.fromisoformat(after[0])
        except ValueError as exc:
            raise ValidationError("cursor is not a cursor this endpoint issued") from exc
        params["after_id"] = after[1]

    statement = LISTED_GRANTS
    if clauses:
        statement += " WHERE " + " AND ".join(clauses)
    statement += " ORDER BY a.created_at DESC, a.id DESC LIMIT :limit"

    rows = [dict(row) for row in (await session.execute(text(statement), params)).mappings()]
    return rows[:limit], len(rows) > limit
