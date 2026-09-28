"""An offering's own weekly hours — its scheduling windows (#199).

**An offering with windows is bookable in them and nowhere else**; its mentor's
general `availability_rules` no longer apply to it, while blocked dates still do
(`SessionTypeSchedulingWindow`'s docstring, and `slot_store` does the swap).

**Reached through the offering, which carries ownership.** Every statement is
scoped with `session_type_of()` — ownership plus soft deletion, not `is_active`,
so a switched-off offering's hours can be edited before it is switched back on —
and a window id is only ever matched together with its offering's id, so one
offering's window cannot be edited through another's URL (non-negotiable #5).

Same row shape as `availability_rules`, so the same write schemas; the overlap
rule is the same too, scoped to the offering.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.db.availability_writer import RULE_COLUMNS, overlap_free
from app.infra.db.models.availability import SessionTypeSchedulingWindow
from app.infra.db.models.sessions import SessionType
from app.infra.db.public_visibility import session_type_of

__all__ = ["create_window", "delete_window", "list_windows", "update_window"]

#: The offering-scoped exclusion constraint, matched to turn an overlap into a 409.
WINDOW_OVERLAP = "session_type_scheduling_windows_no_overlap"

W = SessionTypeSchedulingWindow


def _owned(mentor: UUID, session_type_id: UUID) -> Any:
    """The offering, if it is this mentor's and not deleted — as a subquery."""
    return select(SessionType.id).where(*session_type_of(mentor), SessionType.id == session_type_id)


def _this_window(mentor: UUID, session_type_id: UUID, window_id: UUID) -> list[Any]:
    # `in_(_owned(...))` is both checks at once: the subquery yields this
    # offering's id only if it is the caller's, so a window of another offering
    # — or of this one reached through another mentor — matches nothing.
    return [
        W.id == window_id,
        W.session_type_id.in_(_owned(mentor, session_type_id)),
        W.deleted_at.is_(None),
    ]


async def _owns(session: AsyncSession, mentor: UUID, session_type_id: UUID) -> bool:
    return (await session.execute(_owned(mentor, session_type_id))).first() is not None


async def list_windows(
    session: AsyncSession, mentor: UUID, session_type_id: UUID
) -> list[dict[str, Any]] | None:
    """The offering's live windows by weekday and time, or `None` if not the caller's."""
    if not await _owns(session, mentor, session_type_id):
        return None
    result = await session.execute(
        select(W.id, W.day_of_week, W.start_time, W.end_time, W.timezone, W.is_active)
        .where(W.session_type_id == session_type_id, W.deleted_at.is_(None))
        .order_by(W.day_of_week, W.start_time, W.id)
    )
    return [dict(row) for row in result.mappings()]


async def create_window(
    session: AsyncSession, mentor: UUID, session_type_id: UUID, payload: dict[str, Any]
) -> UUID | None:
    """Add a window; `None` if the offering is not the caller's. 409 on overlap."""
    if not await _owns(session, mentor, session_type_id):
        return None
    values = {key: value for key, value in payload.items() if key in RULE_COLUMNS}
    async with overlap_free(WINDOW_OVERLAP):
        created: UUID = (
            await session.execute(
                insert(W).values(session_type_id=session_type_id, **values).returning(W.id)
            )
        ).scalar_one()
    return created


async def update_window(
    session: AsyncSession,
    mentor: UUID,
    session_type_id: UUID,
    window_id: UUID,
    payload: dict[str, Any],
) -> bool:
    """Change a window; `False` if it is not on the caller's offering."""
    scope = _this_window(mentor, session_type_id, window_id)
    values = {key: value for key, value in payload.items() if key in RULE_COLUMNS}
    if not values:
        return (await session.execute(select(W.id).where(*scope))).first() is not None
    async with overlap_free(WINDOW_OVERLAP):
        result = await session.execute(update(W).where(*scope).values(**values))
    return int(getattr(result, "rowcount", 0) or 0) > 0


async def delete_window(
    session: AsyncSession, mentor: UUID, session_type_id: UUID, window_id: UUID
) -> bool:
    """Soft-delete a window, which frees its hours for a new one at once."""
    result = await session.execute(
        update(W)
        .where(*_this_window(mentor, session_type_id, window_id))
        .values(deleted_at=func.now())
    )
    return int(getattr(result, "rowcount", 0) or 0) > 0
