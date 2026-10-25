"""Which application stages a session type is aimed at — the set (#215).

The #205 shape of `offerings.py`, for the other axis: `session_type_stages`
holds the set in the mentor's order and is the only place stages are read
from (decision 293). The first is still dual-written to the deprecated
`session_types.application_stage`, kept on purpose, so a rollback finds it.
**No rows means any stage.**
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ApplicationStage
from app.infra.db.models.sessions import SessionTypeStage

__all__ = ["stages_for_session_types", "write_session_type_stages"]


async def stages_for_session_types(
    session: AsyncSession, session_type_ids: Sequence[Any]
) -> dict[Any, list[ApplicationStage]]:
    """Each session type's stages, in the mentor's order: **one statement for a
    whole list**, keyed by session type. A type with no rows is absent, which
    reads as any stage.
    """
    if not session_type_ids:
        return {}
    rows = await session.execute(
        select(SessionTypeStage.session_type_id.label("type_id"), SessionTypeStage.stage)
        .where(SessionTypeStage.session_type_id.in_(session_type_ids))
        .order_by(SessionTypeStage.session_type_id, SessionTypeStage.position)
    )
    result: dict[Any, list[ApplicationStage]] = {}
    for row in rows:
        result.setdefault(row.type_id, []).append(ApplicationStage(str(row.stage)))
    return result


async def write_session_type_stages(
    session: AsyncSession, session_type_id: Any, stages: Sequence[ApplicationStage]
) -> None:
    """Replace one session type's rows, in order.

    **Only the rows.** The legacy column is written by the caller, with
    `first_stage`, in the statement that updates the type.
    """
    await session.execute(
        delete(SessionTypeStage).where(SessionTypeStage.session_type_id == session_type_id)
    )
    if stages:
        await session.execute(
            insert(SessionTypeStage),
            [
                {"session_type_id": session_type_id, "stage": stage.value, "position": index}
                for index, stage in enumerate(stages)
            ],
        )
