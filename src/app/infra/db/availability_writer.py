"""Writing a mentor's own availability.

Same shape as `profile_writer`: the owner is a `WHERE` clause on every update
and delete, so a request carrying somebody else's row id under the caller's own
URL changes nothing and reports nothing found. That is the case a mutation batch
found nine endpoints missing in M2 — the dependency passes, and the `WHERE`
clause is all that is left.

**The overlap constraint is what makes this different from the profile writers.**
`availability_rules` carries a partial `EXCLUDE` forbidding two live, active
windows overlapping on one weekday, so an ordinary mentor mistake — dragging a
window across a neighbour — arrives here as an `IntegrityError`. Left unmapped
it is a 500; `overlap_free` turns it into the conflict it actually is.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.infra.db.models.availability import AvailabilityException, AvailabilityRule

__all__ = [
    "create_exception",
    "create_rule",
    "delete_exception",
    "delete_rule",
    "overlap_free",
    "update_rule",
]

#: The constraint added in `b4e8d33a1c72`. Named here because the message is
#: matched against it: a bare `IntegrityError` catch would also swallow the
#: `legacy_bubble_id` unique violation and report an unrelated conflict.
OVERLAP_CONSTRAINT = "availability_rules_no_overlap"

RULE_COLUMNS = frozenset({"day_of_week", "start_time", "end_time", "timezone", "is_active"})


@asynccontextmanager
async def overlap_free() -> AsyncIterator[None]:
    """Turn the exclusion constraint into a 409 rather than a 500.

    The constraint is the mechanism — checking first and inserting after is a
    check-then-insert race, and the guardrail against exactly that is why the
    constraint exists. So the write is attempted and the refusal translated,
    which is the one order that cannot be raced.
    """
    try:
        yield
    except IntegrityError as exc:
        if OVERLAP_CONSTRAINT in str(exc.orig):
            raise ConflictError(
                "this window overlaps one you already have on that day; widen "
                "the existing rule instead of adding a second"
            ) from exc
        raise


async def create_rule(session: AsyncSession, user_id: UUID, payload: dict[str, Any]) -> UUID:
    async with overlap_free():
        result = await session.execute(
            insert(AvailabilityRule)
            .values(
                mentor_user_id=user_id, **{k: v for k, v in payload.items() if k in RULE_COLUMNS}
            )
            .returning(AvailabilityRule.id)
        )
        return result.scalar_one()


async def update_rule(
    session: AsyncSession, user_id: UUID, rule_id: UUID, payload: dict[str, Any]
) -> bool:
    values = {key: value for key, value in payload.items() if key in RULE_COLUMNS}
    if not values:
        found = await session.execute(
            select(AvailabilityRule.id).where(
                AvailabilityRule.id == rule_id,
                AvailabilityRule.mentor_user_id == user_id,
                AvailabilityRule.deleted_at.is_(None),
            )
        )
        return found.first() is not None

    async with overlap_free():
        result = await session.execute(
            update(AvailabilityRule)
            .where(
                AvailabilityRule.id == rule_id,
                AvailabilityRule.mentor_user_id == user_id,
                AvailabilityRule.deleted_at.is_(None),
            )
            .values(**values)
        )
        return _rowcount(result) > 0


async def delete_rule(session: AsyncSession, user_id: UUID, rule_id: UUID) -> bool:
    """Soft. `availability_rules` carries `deleted_at`, and the exclusion
    constraint is partial on it — so a deleted window stops blocking the slot it
    used to occupy, which is the whole reason that predicate is there."""
    result = await session.execute(
        update(AvailabilityRule)
        .where(
            AvailabilityRule.id == rule_id,
            AvailabilityRule.mentor_user_id == user_id,
            AvailabilityRule.deleted_at.is_(None),
        )
        .values(deleted_at=func.now())
    )
    return _rowcount(result) > 0


async def create_exception(session: AsyncSession, user_id: UUID, payload: dict[str, Any]) -> UUID:
    span = func.daterange(payload["start_date"], payload["end_date"], "[)")
    result = await session.execute(
        insert(AvailabilityException)
        .values(
            mentor_user_id=user_id,
            type=payload["type"],
            date_range=span,
            start_time=payload.get("start_time"),
            end_time=payload.get("end_time"),
            timezone=payload["timezone"],
            reason=payload.get("reason"),
        )
        .returning(AvailabilityException.id)
    )
    return result.scalar_one()


async def delete_exception(session: AsyncSession, user_id: UUID, exception_id: UUID) -> bool:
    result = await session.execute(
        update(AvailabilityException)
        .where(
            AvailabilityException.id == exception_id,
            AvailabilityException.mentor_user_id == user_id,
            AvailabilityException.deleted_at.is_(None),
        )
        .values(deleted_at=func.now())
    )
    return _rowcount(result) > 0


def _rowcount(result: Any) -> int:
    return int(result.rowcount or 0)
