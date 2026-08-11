"""Writing availability rules and exceptions, idempotently.

Two tables, both anchored on ``legacy_bubble_id``, both upserted so a re-run is
the recovery plan rather than a rollback.

**The exclusion constraint changes what a bad row costs here.**
``availability_rules`` carries a partial ``EXCLUDE`` forbidding two windows
overlapping on one mentor-weekday, so an unmerged pair does not land as an odd
row — it raises, and takes the transaction with it. The transform merges before
anything reaches this module, and ``plan.merged_overlaps`` records what it did.

``timestamps_from_source`` is not optional. ``trg_set_updated_at`` is a
``BEFORE UPDATE`` trigger, so a first load into empty tables keeps Bubble's
timestamps whether or not it is disabled — and the second load rewrites every
one of them to the import clock. The ETL is required to be re-runnable, so the
second load is the normal path.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain.transform.availability import AvailabilityExceptionRow, AvailabilityRuleRow
from app.infra.db.triggers import timestamps_from_source_across

__all__ = ["AvailabilityLoader"]

#: Both tables held off together: the pair is one logical write, and disabling
#: them separately leaves a window where half the load stamps the import clock.
TABLES = ("availability_rules", "availability_exceptions")

UPSERT_RULE = """
INSERT INTO availability_rules
    (mentor_user_id, day_of_week, start_time, end_time, timezone, is_active,
     created_at, updated_at, legacy_bubble_id)
VALUES
    (:mentor_user_id, :day_of_week, :start_time, :end_time, :timezone, :is_active,
     COALESCE(:created_at, now()), COALESCE(:updated_at, now()), :legacy_bubble_id)
ON CONFLICT (legacy_bubble_id) DO UPDATE SET
    mentor_user_id = EXCLUDED.mentor_user_id,
    day_of_week    = EXCLUDED.day_of_week,
    start_time     = EXCLUDED.start_time,
    end_time       = EXCLUDED.end_time,
    timezone       = EXCLUDED.timezone,
    is_active      = EXCLUDED.is_active,
    updated_at     = EXCLUDED.updated_at
"""

# `CAST(... AS availability_exception_type)` spelled out: the ETL writes raw SQL,
# so nothing in Pydantic or the ORM is between a transform bug and the column.
# The enum is what refuses a value the vocabulary does not contain.
UPSERT_EXCEPTION = """
INSERT INTO availability_exceptions
    (mentor_user_id, type, date_range, timezone, max_sessions_per_day,
     created_at, updated_at, legacy_bubble_id)
VALUES
    (:mentor_user_id, CAST(:kind AS availability_exception_type),
     daterange(:start_date, :end_date, '[)'), :timezone, :max_sessions_per_day,
     COALESCE(:created_at, now()), COALESCE(:updated_at, now()), :legacy_bubble_id)
ON CONFLICT (legacy_bubble_id) DO UPDATE SET
    mentor_user_id       = EXCLUDED.mentor_user_id,
    type                 = EXCLUDED.type,
    date_range           = EXCLUDED.date_range,
    timezone             = EXCLUDED.timezone,
    max_sessions_per_day = EXCLUDED.max_sessions_per_day,
    updated_at           = EXCLUDED.updated_at
"""


class AvailabilityLoader:
    """Both availability tables, rules before exceptions.

    **Returns nothing on purpose.** An earlier version returned a count of
    the rows it was *given*, which the script printed as "loaded N rules" —
    an assertion about the database made without asking it. What actually
    landed is `reconcile_availability`'s answer, read back from the table,
    and two sources for that number is one too many.
    """

    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def load(
        self,
        *,
        users: dict[str, UUID],
        rules: Sequence[AvailabilityRuleRow],
        exceptions: Sequence[AvailabilityExceptionRow],
    ) -> None:
        def mentor_id(bubble_id: str, what: str) -> UUID:
            resolved = users.get(bubble_id)
            if resolved is None:
                # The transform already refuses an owner with no mentor profile,
                # so reaching this means the two disagree about who exists.
                # Raising is the point: skipping silently would drop a mentor's
                # availability with nothing to show for it.
                raise LookupError(f"{what} references unknown user {bubble_id}")
            return resolved

        async with timestamps_from_source_across(self._connection, TABLES):
            for rule in rules:
                await self._connection.execute(
                    text(UPSERT_RULE),
                    {
                        "mentor_user_id": mentor_id(rule.mentor_bubble_id, "availability rule"),
                        "day_of_week": rule.day_of_week,
                        "start_time": rule.start_time,
                        "end_time": rule.end_time,
                        "timezone": rule.timezone,
                        "is_active": rule.is_active,
                        "created_at": rule.created_at,
                        "updated_at": rule.updated_at,
                        "legacy_bubble_id": rule.legacy_bubble_id,
                    },
                )

            for exception in exceptions:
                await self._connection.execute(
                    text(UPSERT_EXCEPTION),
                    {
                        "mentor_user_id": mentor_id(
                            exception.mentor_bubble_id, "availability exception"
                        ),
                        "kind": exception.kind.value,
                        "start_date": exception.start_date,
                        "end_date": exception.end_date,
                        "timezone": exception.timezone,
                        "max_sessions_per_day": exception.max_sessions_per_day,
                        "created_at": exception.created_at,
                        "updated_at": exception.updated_at,
                        "legacy_bubble_id": exception.legacy_bubble_id,
                    },
                )
