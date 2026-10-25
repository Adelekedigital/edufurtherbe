"""How many session types a mentor may hold, and how many they do (decision 286).

**One expression for the limit.** `effective_limit()` is the `COALESCE` of the
support override and the domain default; the create guard, the admin answer
and `GET /me` all read it, so the three cannot disagree.

**Counted under a per-mentor advisory lock**, taken by `refuse_if_full`
itself so the count cannot be read without it. A `CHECK` sees one row, so the
count is the writer's, and two creates at the edge must not both pass it. An
advisory lock rather than `lock_mentor`'s row lock: a keyed create has already
claimed its `Idempotency-Key`, whose row holds `FOR KEY SHARE` on the user, and
`FOR UPDATE` on that user would deadlock two keyed creates against each other.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, literal, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.errors import NotFoundError, SessionTypeLimitReachedError
from app.domain.session_type_limit import DEFAULT_SESSION_TYPE_LIMIT
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.sessions import SessionType

__all__ = [
    "SESSION_TYPE_LIMIT_LOCK_NAMESPACE",
    "effective_limit",
    "live_profile",
    "refuse_if_full",
    "set_session_type_limit",
]

#: Namespace for this module's advisory lock: `'STYP'` as four ASCII bytes.
SESSION_TYPE_LIMIT_LOCK_NAMESPACE = 0x53545950


def effective_limit() -> ColumnElement[int]:
    """The mentor's limit: support's override, else the default."""
    return func.coalesce(MentorProfile.session_type_limit, literal(DEFAULT_SESSION_TYPE_LIMIT))


async def _lock(session: AsyncSession, mentor_user_id: UUID) -> None:
    """Serialise one mentor's creates and limit changes. Held to transaction end."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:ns, hashtext(:key))"),
        {"ns": SESSION_TYPE_LIMIT_LOCK_NAMESPACE, "key": str(mentor_user_id)},
    )


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


def live_profile(user_id: UUID) -> list[ColumnElement[bool]]:
    """The user's mentor profile, not deleted: the one test of "is a mentor"
    the create, its guard and the admin write share."""
    return [MentorProfile.user_id == user_id, MentorProfile.deleted_at.is_(None)]


async def refuse_if_full(session: AsyncSession, mentor_user_id: UUID) -> None:
    """Lock the mentor's creates, then refuse a new offering at the limit.

    The lock is taken here, not by the caller, so no path can count without it.
    """
    await _lock(session, mentor_user_id)
    row = (
        await session.execute(
            select(effective_limit(), _held()).where(*live_profile(mentor_user_id))
        )
    ).first()
    if row is None:  # pragma: no cover - the caller found it in this transaction
        raise NotFoundError("this user has no mentor profile")
    limit, held = row
    if held >= limit:
        raise SessionTypeLimitReachedError(
            f"You can have up to {limit} session types. To add another, delete one "
            "with no booked sessions; one scheduled for deletion still counts until "
            "its last session is over.",
            limit=limit,
        )


async def set_session_type_limit(
    session: AsyncSession, user_id: UUID, limit: int | None
) -> tuple[int, bool] | None:
    """Set one mentor's override (`None` resets to the default).

    Returns the effective limit and whether it is the default, or `None` for a
    user with no live mentor profile. Deletes nothing: a limit below what the
    mentor holds only refuses new creates. Under the create's lock, so a create
    in flight is judged against one limit or the other. Does not commit.
    """
    await _lock(session, user_id)
    row = (
        await session.execute(
            update(MentorProfile)
            .where(*live_profile(user_id))
            .values(session_type_limit=limit)
            .returning(effective_limit(), MentorProfile.session_type_limit.is_(None))
        )
    ).first()
    if row is None:
        return None
    return int(row[0]), bool(row[1])
