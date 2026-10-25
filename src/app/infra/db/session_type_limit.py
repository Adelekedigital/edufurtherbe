"""How many session types a mentor may hold, and how many they do (decision 285).

**One expression for the limit.** `effective_limit()` is the `COALESCE` of the
support override and the domain default; the create guard, the admin answer
and `GET /me` all read it, so the three cannot disagree.

**Counted under `lock_mentor`**, taken by `create_session_type` before it
calls `refuse_if_full`. A `CHECK` sees one row, so the count is the writer's,
and two creates at the edge must not both pass it.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.errors import SessionTypeLimitReachedError
from app.domain.session_type_limit import DEFAULT_SESSION_TYPE_LIMIT
from app.infra.db.mentor_status_store import lock_mentor
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.sessions import SessionType

__all__ = ["effective_limit", "refuse_if_full", "set_session_type_limit"]


def effective_limit() -> ColumnElement[int]:
    """The mentor's limit: support's override, else the default."""
    return func.coalesce(MentorProfile.session_type_limit, literal(DEFAULT_SESSION_TYPE_LIMIT))


def _held() -> ColumnElement[int]:
    """Every offering not truly deleted: live, hidden and scheduled alike."""
    return (
        select(func.count())
        .select_from(SessionType)
        .where(
            SessionType.mentor_user_id == MentorProfile.user_id,
            SessionType.deleted_at.is_(None),
        )
        .correlate(MentorProfile)
        .scalar_subquery()
    )


async def refuse_if_full(session: AsyncSession, mentor_user_id: UUID) -> None:
    """Refuse a new offering when the mentor holds their limit already.

    The caller holds `lock_mentor`; without it two creates both read one
    below the limit and both land.
    """
    row = (
        await session.execute(
            select(effective_limit(), _held()).where(
                MentorProfile.user_id == mentor_user_id, MentorProfile.deleted_at.is_(None)
            )
        )
    ).first()
    if row is None:  # pragma: no cover - the caller has just locked the live profile
        return
    limit, held = row
    if held >= limit:
        raise SessionTypeLimitReachedError(
            f"You can have up to {limit} session types. Delete one to add another.",
            limit=limit,
        )


async def set_session_type_limit(
    session: AsyncSession, user_id: UUID, limit: int | None
) -> tuple[int, bool] | None:
    """Set one mentor's override (`None` resets to the default).

    Returns the effective limit and whether it is the default, or `None` for a
    user with no live mentor profile. Deletes nothing: a limit below what the
    mentor holds only refuses new creates. Does not commit.

    `lock_mentor` first, the lock every create takes, so a create in flight
    is judged against one limit or the other, never half of each.
    """
    if not await lock_mentor(session, user_id):
        return None
    row = (
        await session.execute(
            update(MentorProfile)
            .where(MentorProfile.user_id == user_id, MentorProfile.deleted_at.is_(None))
            .values(session_type_limit=limit)
            .returning(effective_limit(), MentorProfile.session_type_limit.is_(None))
        )
    ).first()
    if row is None:  # pragma: no cover - the live profile was just locked
        return None
    return int(row[0]), bool(row[1])
