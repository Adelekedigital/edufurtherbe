"""A party's reason for missing a session: sending it, and the admin read.

**"Missed" is `session_stats.missed_by()`, imported, never retyped**: the
caller's own participant row says `no_show` on a session whose status is
`completed` or `no_show`. It is the predicate behind
`missed_by_mentor` and the attendance rate, so a party may explain exactly the
miss their figures already count.

**Scoped in the statement** (non-negotiable #5): the session is read with
`is_a_party(caller)`, so somebody else's session is absent rather than fetched
and refused, and `user_id` is always the caller's own, from the token.

**The other party never reads a reason.** Nothing here returns one to a party,
and `SessionRead` carries only whether the *viewer* sent theirs
(`session_store`).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import case, insert, literal, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    MissedReasonAlreadySentError,
    MissedReasonNotYoursError,
    NotFoundError,
    SessionNotMissedError,
)
from app.domain.enums import MissedReason, SessionRole
from app.infra.db.models.missed_reasons import SessionMissedReason
from app.infra.db.models.sessions import Session
from app.infra.db.session_stats import missed_by
from app.infra.db.session_store import is_a_party

__all__ = ["ONE_PER_PARTY", "missed_reasons_of", "send_missed_reason"]

#: The pair a second send lands on, matched by name so a different integrity
#: failure is re-raised rather than reported as a duplicate (decision 169).
ONE_PER_PARTY = "uq_session_missed_reasons_session_user"

_READ = (SessionMissedReason.reason, SessionMissedReason.note, SessionMissedReason.created_at)


async def send_missed_reason(
    session: AsyncSession,
    session_id: UUID,
    caller: UUID,
    *,
    reason: MissedReason,
    note: str | None,
) -> dict[str, Any]:
    """Record why ``caller`` missed ``session_id``, or say why not. Does not commit.

    Raises, in this order: :class:`NotFoundError` when the session is not the
    caller's (or does not exist); :class:`SessionNotMissedError` when it is not
    settled, or nobody missed it (both attended); :class:`MissedReasonNotYoursError`
    when the other party missed it and the caller did not;
    :class:`MissedReasonAlreadySentError` on a second send.

    **The unique pair is the only duplicate check.** A pre-check would race
    anyway, so the insert is the test, and its violation is caught by name and
    rolled back here, since the transaction is already aborted (decision 169).
    """
    target = (
        await session.execute(
            select(
                missed_by(caller).label("missed"),
                # Either party, by the same predicate: `missed_by()` already
                # requires a settled status, so this is "settled as missed".
                or_(missed_by(Session.mentor_id), missed_by(Session.mentee_id)).label("anyone"),
            ).where(Session.id == session_id, is_a_party(caller))
        )
    ).one_or_none()
    if target is None:
        raise NotFoundError("no such session")
    if not target.anyone:
        raise SessionNotMissedError("nobody missed this session, or it is not settled yet")
    if not target.missed:
        raise MissedReasonNotYoursError("you did not miss this session")

    try:
        written = await session.execute(
            insert(SessionMissedReason)
            .values(session_id=session_id, user_id=caller, reason=reason, note=note)
            .returning(*_READ)
        )
    except IntegrityError as exc:
        if ONE_PER_PARTY not in str(exc.orig):
            raise
        await session.rollback()
        raise MissedReasonAlreadySentError("you have already said why") from exc
    return dict(written.mappings().one())


async def missed_reasons_of(session: AsyncSession, session_id: UUID) -> list[dict[str, Any]] | None:
    """Every reason sent for one session, oldest first, with the sender's role.
    ``None`` when there is no such session. **Admins only**: the caller's
    grant is checked by the dependency, and no party reaches this."""
    found = await session.scalar(select(Session.id).where(Session.id == session_id))
    if found is None:
        return None
    rows = await session.execute(
        select(
            SessionMissedReason.user_id,
            # The writer only accepts a party, so the sender is one side or the other.
            case(
                (
                    SessionMissedReason.user_id == Session.mentor_id,
                    literal(SessionRole.MENTOR.value),
                ),
                else_=literal(SessionRole.MENTEE.value),
            ).label("role"),
            *_READ,
        )
        .join(Session, Session.id == SessionMissedReason.session_id)
        .where(SessionMissedReason.session_id == session_id)
        .order_by(SessionMissedReason.created_at, SessionMissedReason.id)
    )
    return [dict(row) for row in rows.mappings()]
