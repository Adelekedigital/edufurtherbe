"""Which application stages a session type is aimed at — the set (#212).

The #205 shape of `offerings.py`, for the other axis: `session_type_stages`
holds the set in the mentor's order, and the first is dual-written to
`session_types.application_stage` so code from before the set keeps reading it.
**No rows means any stage.**
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, exists, insert, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ApplicationStage
from app.infra.db.models.sessions import SessionType, SessionTypeStage

__all__ = ["legacy_stage", "stages_for_session_types", "write_session_type_stages"]


def legacy_stage(stages: Sequence[ApplicationStage]) -> ApplicationStage | None:
    """What `session_types.application_stage` holds for this set: its first."""
    return stages[0] if stages else None


async def stages_for_session_types(
    session: AsyncSession, session_type_ids: Sequence[Any]
) -> dict[Any, list[ApplicationStage]]:
    """Each session type's stages, in the mentor's order.

    **One statement for a whole list**, keyed by session type. A type with rows
    reads them; **a type with none falls back to the legacy column** — which is
    what code from before the set, the demo seed and the ETL still write, so a
    single-stage offering reads the same as it did.
    """
    if not session_type_ids:
        return {}
    joined = select(
        SessionTypeStage.session_type_id.label("type_id"),
        SessionTypeStage.stage,
        SessionTypeStage.position,
    ).where(SessionTypeStage.session_type_id.in_(session_type_ids))
    legacy = select(
        SessionType.id.label("type_id"),
        SessionType.application_stage.label("stage"),
        literal(0).label("position"),
    ).where(
        SessionType.id.in_(session_type_ids),
        SessionType.application_stage.is_not(None),
        ~exists().where(SessionTypeStage.session_type_id == SessionType.id),
    )
    rows = await session.execute(joined.union_all(legacy).order_by("type_id", "position"))
    result: dict[Any, list[ApplicationStage]] = {}
    for row in rows:
        result.setdefault(row.type_id, []).append(ApplicationStage(str(row.stage)))
    return result


async def write_session_type_stages(
    session: AsyncSession, session_type_id: Any, stages: Sequence[ApplicationStage]
) -> None:
    """Replace one session type's rows, in order.

    **Only the rows.** The legacy column is written by the caller in the same
    statement as `custom_stage_label` — `legacy_stage` says what — because the
    `CHECK` tying the two is immediate: writing the stage and the label in two
    statements refuses a legal change halfway through it.
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
