"""Reading a mentor's declared availability.

**Every statement carries the owner and the soft-delete predicate**, in the
`WHERE` clause rather than in a check the caller makes afterwards. A hidden row
is not a protected row: this project has already shipped a list endpoint that
scoped correctly beside an action endpoint that reached other owners' rows by id.

`deleted_at IS NULL` appears once per statement here and nowhere else. That
phrase has been hand-typed into five statements in this codebase before, with
the fifth forgotten — so `test_profile_store_soft_deletes` derives the list of
tables that need checking from `Base.metadata` rather than from anything a
person maintains, and both of these tables are now in it.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.db.models.availability import AvailabilityException, AvailabilityRule

__all__ = ["get_rule", "list_exceptions", "list_rules"]


async def list_rules(session: AsyncSession, user_id: UUID) -> list[dict[str, Any]]:
    """Every live rule for one mentor, ordered as a schedule reads."""
    result = await session.execute(
        select(
            AvailabilityRule.id,
            AvailabilityRule.day_of_week,
            AvailabilityRule.start_time,
            AvailabilityRule.end_time,
            AvailabilityRule.timezone,
            AvailabilityRule.is_active,
        )
        .where(
            AvailabilityRule.mentor_user_id == user_id,
            AvailabilityRule.deleted_at.is_(None),
        )
        .order_by(AvailabilityRule.day_of_week, AvailabilityRule.start_time)
    )
    return [dict(row) for row in result.mappings()]


async def get_rule(session: AsyncSession, user_id: UUID, rule_id: UUID) -> dict[str, Any] | None:
    """One rule, scoped to its owner.

    ``user_id`` is in the `WHERE` clause, not compared afterwards — which is the
    difference between "this row is not yours" and "this row does not exist",
    and the API deliberately answers both the same way.
    """
    result = await session.execute(
        select(
            AvailabilityRule.id,
            AvailabilityRule.day_of_week,
            AvailabilityRule.start_time,
            AvailabilityRule.end_time,
            AvailabilityRule.timezone,
            AvailabilityRule.is_active,
        ).where(
            AvailabilityRule.id == rule_id,
            AvailabilityRule.mentor_user_id == user_id,
            AvailabilityRule.deleted_at.is_(None),
        )
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def list_exceptions(session: AsyncSession, user_id: UUID) -> list[dict[str, Any]]:
    result = await session.execute(
        # `lower()`/`upper()` rather than the range object itself. A `Range` is a
        # SQLAlchemy type, and `api/` must stay free of the database framework —
        # handing one to a response model made the schema import
        # `sqlalchemy.dialects.postgresql`, which `check_layers` refused. Taking
        # the bounds apart here is where that knowledge belongs anyway, and it
        # puts two plain dates in front of the schema instead of a driver object
        # whose bounds are independently nullable.
        select(
            AvailabilityException.id,
            AvailabilityException.type,
            func.lower(AvailabilityException.date_range).label("start_date"),
            func.upper(AvailabilityException.date_range).label("end_date"),
            AvailabilityException.start_time,
            AvailabilityException.end_time,
            AvailabilityException.timezone,
            AvailabilityException.reason,
        )
        .where(
            AvailabilityException.mentor_user_id == user_id,
            AvailabilityException.deleted_at.is_(None),
        )
        .order_by(AvailabilityException.date_range)
    )
    return [dict(row) for row in result.mappings()]
