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
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    and_,
    case,
    exists,
    func,
    insert,
    not_,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.errors import ValidationError
from app.domain.enums import ApprovalStatus, ListingStatus, MentorStatusType, UnlistedReason
from app.domain.listing import (
    RETURN_ON_POINTER,
    RETURN_REMINDER_OFFSETS,
    first_reminder_stage,
    return_on_problem,
    stage_after,
    stage_before,
)
from app.domain.notifications import Notification
from app.infra.db.mentor_listing import (
    newest_unlisting_is_self,
    paused_by_mentor,
    reminder_eligible,
    return_reminder_due,
)
from app.infra.db.models.mentoring import MentorProfile, MentorStatusEvent
from app.infra.db.models.user import User
from app.infra.db.outbox import enqueue
from app.infra.db.predicates import LIVE


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
    if not await _lock(session, user_id):
        return False

    await session.execute(
        insert(MentorStatusEvent).values(
            mentor_user_id=user_id,
            status_type=status_type,
            reason=reason,
            created_by=created_by,
            # **Clock time, not the transaction's start.** Transitions on one
            # mentor are serialised by the row lock above, and stamping when the
            # event is written — after the lock — keeps "newest" equal to "last
            # committed". `now()` would let a transaction that started first but
            # waited on the lock look older than the one it followed.
            created_at=func.clock_timestamp(),
        )
    )
    # A `listed` event also clears the pause's return date — in the projection
    # trigger, so a listing written by any path ends the pause (#226).
    return True


async def _lock(session: AsyncSession, user_id: UUID) -> bool:
    """Lock this mentor's live profile row for the transition. ``False`` if none.

    **Every transition takes it first**, so two on one mentor run one after the
    other and a decision read before writing — the pause guard — cannot be
    overtaken by an admin's unlisting committing in between (#225).
    """
    # The account's row as well as the profile's: a mentor whose account is
    # soft-deleted after the caller was authenticated has no transition left
    # to make, and holding the user row stops the delete crossing the write.
    found = await session.execute(
        select(MentorProfile.user_id)
        .join(User, User.id == MentorProfile.user_id)
        .where(MentorProfile.user_id == user_id, MentorProfile.deleted_at.is_(None), LIVE)
        .with_for_update()
    )
    return found.first() is not None


async def _set_return(
    session: AsyncSession, user_id: UUID, return_on: dt.date | None, stage: int | None
) -> bool:
    """Set the pause's return date and its first pending reminder. ``False`` if gone."""
    changed = await session.execute(
        update(MentorProfile)
        .where(MentorProfile.user_id == user_id, MentorProfile.deleted_at.is_(None))
        .values(return_on=return_on, return_reminder_stage=stage)
    )
    return bool(changed.rowcount)  # type: ignore[attr-defined]


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
    # Locked before the approval is read, so an account deleted meanwhile is
    # absent here rather than a decision that reports success and records
    # nothing (`record` would then refuse both events).
    if not await _lock(session, user_id):
        return False
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


def _unlisted_by_someone_else() -> ColumnElement[bool]:
    """Currently unlisted by an admin (or a decline): the pause guard's refusal.

    A new profile's default `unlisted` has no unlisting event at all, so a
    pending applicant may still pause — the guard refuses an *admin's* unlisting,
    not the starting state.
    """
    event = aliased(MentorStatusEvent)
    has_unlisting = exists(
        select(event.id)
        .where(
            event.mentor_user_id == MentorProfile.user_id,
            event.status_type == MentorStatusType.UNLISTED,
        )
        .correlate(MentorProfile)
    )
    return and_(
        MentorProfile.listing_status == ListingStatus.UNLISTED,
        has_unlisting,
        not_(newest_unlisting_is_self()),
    )


async def _profile_flag(session: AsyncSession, user_id: UUID, flag: Any) -> bool | None:
    """One flag about this mentor's live profile, or ``None`` with no profile."""
    row = (
        await session.execute(
            select(flag).where(MentorProfile.user_id == user_id, MentorProfile.deleted_at.is_(None))
        )
    ).first()
    return None if row is None else bool(row[0])


async def unlisted_by_someone_else(session: AsyncSession, user_id: UUID) -> bool:
    """Whether a pause must be refused: an admin's unlisting stands (#75)."""
    return bool(await _profile_flag(session, user_id, _unlisted_by_someone_else()))


#: What a pause attempt came to.
PauseOutcome = Literal["paused", "refused", "absent"]


async def pause(
    session: AsyncSession,
    *,
    user_id: UUID,
    now: dt.datetime,
    return_on: dt.date | None = None,
) -> PauseOutcome:
    """A mentor taking themselves out of the listing, optionally saying when
    they expect to be back.

    `created_by` is the mentor: they are the actor, and with the reason that is
    what distinguishes this from an admin unlisting the same row.

    **`refused` while an admin's unlisting stands** (#225): pausing over it would
    make the newest unlisting the mentor's own and launder it into a resume. The
    profile row is locked **before** that check and held through the write, so
    an admin's unlisting cannot commit in between.

    **Already paused by themselves: only the date changes.** "Change return
    date" calls this again, and a second `unlisted` event would record a
    transition that never happened.
    """
    if not await _lock(session, user_id):
        return "absent"
    if await unlisted_by_someone_else(session, user_id):
        return "refused"
    # **The write flow enforces the date rule, not the transport** — any caller
    # of `pause` gets it. The mentor's today is theirs, so it is read here.
    local_now = await local_time(session, user_id, now)
    if local_now is None:
        return "absent"
    problem = return_on_problem(return_on, local_now.date())
    if problem is not None:
        raise ValidationError(problem, field_errors=((RETURN_ON_POINTER, problem),))
    already = await _profile_flag(session, user_id, paused_by_mentor())
    if not already:
        await record(
            session,
            user_id=user_id,
            status_type=MentorStatusType.UNLISTED,
            created_by=user_id,
            reason=UnlistedReason.MENTOR_PAUSED.value,
        )
    # The stages for this date start again, skipping any already behind us.
    stage = None if return_on is None else first_reminder_stage(return_on, local_now)
    if not await _set_return(session, user_id, return_on, stage):
        return "absent"
    return "paused"


async def local_time(session: AsyncSession, user_id: UUID, now: dt.datetime) -> dt.datetime | None:
    """This user's wall-clock time in their own zone (naive), or ``None`` with no
    live account.

    A return date is a date in the mentor's zone, so "after today" has to be
    their today — UTC's would refuse a mentor in Auckland their own tomorrow —
    and which reminder stages are still ahead is measured on the same clock.
    """
    row = (
        await session.execute(
            select(func.timezone(User.timezone, now)).where(User.id == user_id, LIVE)
        )
    ).first()
    return None if row is None else row[0]


async def remind_returning_mentors(session: AsyncSession, *, now: dt.datetime) -> int:
    """Queue each self-paused mentor's return reminder whose stage has come.

    **A reminder, never a switch** (Calendar request, 2026-10-01): nobody is
    relisted here. One template on three stages (`RETURN_REMINDER_OFFSETS`: a week, three days, the
    day), each at `RETURN_REMINDER_HOUR` in the mentor's own zone — the first
    hourly run at or after it — while they are still paused by themselves.

    **Claimed in one `UPDATE … RETURNING`**, which is what makes each stage
    send once: the claim steps the pending stage to the next, so two
    overlapping runs cannot both claim it and a re-run finds it moved on. Does
    not commit.
    """
    following = case(
        {offset: stage_after(offset) for offset in RETURN_REMINDER_OFFSETS},
        value=MentorProfile.return_reminder_stage,
        else_=None,
    )
    claimed = (
        await session.execute(
            update(MentorProfile)
            .where(
                User.id == MentorProfile.user_id,
                reminder_eligible(),
                MentorProfile.return_reminder_stage.is_not(None),
                return_reminder_due(now),
            )
            .values(return_reminder_stage=following)
            .returning(
                MentorProfile.user_id,
                MentorProfile.return_on,
                MentorProfile.return_reminder_stage,
            )
        )
    ).all()
    for user_id, return_on, pending in claimed:
        sent = stage_before(pending)
        await enqueue(
            session,
            Notification.MENTOR_RETURN_REMINDER,
            entity_type="mentor_profile",
            entity_id=user_id,
            recipient_ids=(user_id,),
            variables={"return_on": return_on.isoformat(), "stage": str(sent)},
        )
    return len(claimed)


async def may_self_resume(session: AsyncSession, user_id: UUID) -> bool:
    """Whether this mentor may put themselves back on the list.

    **Only if they were the one who took themselves off** (`newest_unlisting_is_self`).
    Otherwise a suspension is a button the suspended person can press — and an
    admin unlisting somebody for review would be undone by the person under
    review.

    Approval matters too: a mentor who was never approved has nothing to return
    to, and relisting them would put an unapproved profile in the directory.
    """
    allowed = and_(
        MentorProfile.approval_status == ApprovalStatus.APPROVED, newest_unlisting_is_self()
    )
    return bool(await _profile_flag(session, user_id, allowed))


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
