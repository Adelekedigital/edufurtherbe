"""Recording a mentor's status transitions, and reading their history.

**Everything here writes an event and nothing writes a status column.**
`trg_apply_mentor_status` projects each event onto `mentor_profiles`, so the
column follows without any caller remembering to update it. That is the whole
design: a helper everybody must remember to call is what this repository has
been burned by four times, starting with `deleted_at IS NULL` typed into five
statements and missed on the fifth.

It lives apart from `admin_store` because both sides use it — an admin
approving, declining or unlisting, and a mentor pausing themselves. A module
named for the actor would have one of them importing the other's.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ApprovalStatus, MentorStatusType, UnlistedReason
from app.domain.notifications import Notification
from app.infra.db.models.mentoring import MentorProfile, MentorStatusEvent
from app.infra.db.models.user import User
from app.infra.db.outbox import enqueue


async def _mentor_exists(session: AsyncSession, user_id: UUID) -> bool:
    found = await session.execute(
        select(MentorProfile.user_id).where(
            MentorProfile.user_id == user_id, MentorProfile.deleted_at.is_(None)
        )
    )
    return found.first() is not None


async def _approval_before(session: AsyncSession, user_id: UUID) -> str | None:
    """This mentor's current approval status, or ``None`` if there is no profile.

    **Read before the decision is recorded**, because that is the only moment
    the previous state is still knowable: `trg_apply_mentor_status` projects
    each event onto the column, so by the time `record` returns the column
    already says what was just decided.

    It is what makes deciding twice quiet. The endpoint does not refuse a second
    decision — the event log is an append-only record of what admins did, and an
    admin approving an already-approved mentor genuinely did that — but the
    *mentor* has no news, and telling them again is the failure an admin who
    double-clicks would cause.
    """
    return (
        await session.execute(
            select(MentorProfile.approval_status).where(
                MentorProfile.user_id == user_id, MentorProfile.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()


async def record(
    session: AsyncSession,
    *,
    user_id: UUID,
    status_type: MentorStatusType,
    created_by: UUID | None,
    reason: str | None = None,
) -> bool:
    """Write one transition. ``False`` when there is no such mentor profile.

    The status column is **not** touched here. The trigger does it, which is why
    the test for this inserts an event directly rather than calling this
    function — otherwise a broken trigger and a working caller look identical.
    """
    if not await _mentor_exists(session, user_id):
        return False

    await session.execute(
        insert(MentorStatusEvent).values(
            mentor_user_id=user_id,
            status_type=status_type,
            reason=reason,
            created_by=created_by,
        )
    )
    return True


async def decide(
    session: AsyncSession,
    *,
    user_id: UUID,
    admin_id: UUID,
    approved: bool,
    reason: str | None = None,
) -> bool:
    """Approve or decline, and list or unlist to match.

    **Two events, not one.** Approval and listing are separate dimensions, and a
    row that stated both would have to copy one forward — which is how two
    concurrent transitions record a state that never existed. Writing them as
    two facts costs one insert and keeps every row true on its own.
    """
    before = await _approval_before(session, user_id)
    if before is None:
        return False

    await record(
        session,
        user_id=user_id,
        status_type=MentorStatusType.APPROVED if approved else MentorStatusType.DECLINED,
        created_by=admin_id,
        reason=None if approved else reason,
    )
    await record(
        session,
        user_id=user_id,
        status_type=MentorStatusType.LISTED if approved else MentorStatusType.UNLISTED,
        created_by=admin_id,
        reason=None if approved else UnlistedReason.NEVER_APPROVED.value,
    )

    # **After the decision, and only when it is news.** After, because a message
    # about a write that failed is worse than silence — they are one transaction,
    # so neither can happen without the other. Only when it changed, because the
    # endpoint permits a second decision and an admin who double-clicks must not
    # send a second email.
    #
    # `recipients` is not used and cannot be: it answers *which party to a
    # session*, and raises for a message that is not about one. There is a single
    # recipient here and it is the applicant.
    decided = ApprovalStatus.APPROVED if approved else ApprovalStatus.DECLINED
    if before != decided.value:
        await enqueue(
            session,
            Notification.MENTOR_APPROVED if approved else Notification.MENTOR_DECLINED,
            entity_type="mentor_profile",
            entity_id=user_id,
            recipient_ids=(user_id,),
            # **Nothing on an approval.** `reason` is already withheld from the
            # approval event above for the same reason: there is no such thing as
            # a reason for being approved, and a decline reason arriving on an
            # approval would be somebody else's words in the wrong message.
            variables=None if approved else {"reason": reason or ""},
        )
    return True


async def set_listing(
    session: AsyncSession,
    *,
    user_id: UUID,
    admin_id: UUID,
    listed: bool,
    reason: str | None = None,
) -> bool:
    """An admin listing or unlisting a mentor, without touching their approval.

    This is the transition that made the log necessary: before it, listing only
    ever moved as a side effect of a decision, so "who unlisted this" was always
    derivable from the approval. It no longer is.
    """
    return await record(
        session,
        user_id=user_id,
        status_type=MentorStatusType.LISTED if listed else MentorStatusType.UNLISTED,
        created_by=admin_id,
        reason=reason if not listed else None,
    )


async def last_unlisting_reason(session: AsyncSession, user_id: UUID) -> str | None:
    """Why this mentor is currently unlisted, from the newest unlisting.

    Read rather than stored: `unlisted_reason` was a column describing only the
    most recent unlisting, which is the duplication the log replaced.
    """
    found = await session.execute(
        select(MentorStatusEvent.reason)
        .where(
            MentorStatusEvent.mentor_user_id == user_id,
            MentorStatusEvent.status_type == MentorStatusType.UNLISTED,
        )
        .order_by(MentorStatusEvent.created_at.desc())
        .limit(1)
    )
    row = found.first()
    return None if row is None else row[0]


async def pause(session: AsyncSession, *, user_id: UUID) -> bool:
    """A mentor taking themselves out of the listing.

    `created_by` is the mentor: they are the actor, and the reason distinguishes
    this from an admin unlisting the same row.
    """
    return await record(
        session,
        user_id=user_id,
        status_type=MentorStatusType.UNLISTED,
        created_by=user_id,
        reason=UnlistedReason.MENTOR_PAUSED.value,
    )


async def may_self_resume(session: AsyncSession, user_id: UUID) -> bool:
    """Whether this mentor may put themselves back on the list.

    **Only if they were the one who took themselves off.** Otherwise a
    suspension is a button the suspended person can press — and an admin
    unlisting somebody for review would be undone by the person under review.

    Approval matters too: a mentor who was never approved has nothing to return
    to, and relisting them would put an unapproved profile in the directory.
    """
    approved = await session.execute(
        select(MentorProfile.approval_status).where(
            MentorProfile.user_id == user_id, MentorProfile.deleted_at.is_(None)
        )
    )
    row = approved.first()
    if row is None or row[0] is not ApprovalStatus.APPROVED:
        return False

    return await last_unlisting_reason(session, user_id) == UnlistedReason.MENTOR_PAUSED.value


async def resume(session: AsyncSession, *, user_id: UUID) -> bool:
    """A mentor returning to the listing after pausing themselves."""
    return await record(
        session,
        user_id=user_id,
        status_type=MentorStatusType.LISTED,
        created_by=user_id,
    )


async def history(
    session: AsyncSession,
    user_id: UUID,
    *,
    limit: int,
    kinds: Sequence[MentorStatusType] = (),
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """One mentor's transitions, newest first, narrowed by kind and by date.

    **The filters are optional and compose.** A reviewer asking *"why was this
    mentor unlisted in March"* is filtering both at once, and a log you can only
    read whole is a log nobody reads past the first page.

    ``kinds`` empty means every kind, which is what a caller who sends no
    ``status`` gets. Spelled as an empty sequence rather than ``None`` because
    "no filter" and "filter by nothing" would otherwise be the same value with
    opposite meanings — and the second is what a client sending an empty list
    means, which is a request for no rows.

    **The window is half-open, ``[since, until)``**, so two adjacent ranges
    partition the log with no row in both and none missed. A closed upper bound
    would return an event landing exactly on midnight in both March and April.

    Served by ``ix_mentor_status_events_mentor``, which is ``(mentor_user_id,
    created_at DESC)`` — the date range rides the index and the kind filter is a
    residual predicate over what is left, which at a few dozen events per mentor
    is the right way round.
    """
    statement = (
        select(
            MentorStatusEvent.id,
            MentorStatusEvent.status_type,
            MentorStatusEvent.reason,
            MentorStatusEvent.created_at,
            MentorStatusEvent.created_by,
            User.email.label("created_by_email"),
        )
        .outerjoin(User, User.id == MentorStatusEvent.created_by)
        .where(MentorStatusEvent.mentor_user_id == user_id)
    )
    if kinds:
        statement = statement.where(MentorStatusEvent.status_type.in_(tuple(kinds)))
    if since is not None:
        statement = statement.where(MentorStatusEvent.created_at >= since)
    if until is not None:
        statement = statement.where(MentorStatusEvent.created_at < until)

    statement = statement.order_by(MentorStatusEvent.created_at.desc()).limit(limit)
    return [dict(row) for row in (await session.execute(statement)).mappings()]
