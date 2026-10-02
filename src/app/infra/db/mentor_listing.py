"""Who is paused, by whom: the listing predicates the store and the outbox share.

Its own module so the outbox — which `mentor_status_store` writes to — can ask
whether a queued return reminder is still due without importing the store back.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import ColumnElement, Integer, Time, and_, case, exists, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.domain.enums import ApprovalStatus, ListingStatus, MentorStatusType, UnlistedReason
from app.domain.listing import (
    RETURN_REMINDER_HOUR,
    cadence,
    latest_due_stage,
    reminder_due_at,
    stage_after,
)
from app.infra.db.models.mentoring import MentorProfile, MentorStatusEvent
from app.infra.db.models.user import User
from app.infra.db.predicates import LIVE


def _newest_unlisting_id() -> Any:
    """The id of this mentor's newest unlisting, correlated to `MentorProfile`."""
    newest = aliased(MentorStatusEvent)
    return (
        select(newest.id)
        .where(
            newest.mentor_user_id == MentorProfile.user_id,
            newest.status_type == MentorStatusType.UNLISTED,
        )
        .order_by(newest.created_at.desc(), newest.id.desc())
        .limit(1)
        .correlate(MentorProfile)
        .scalar_subquery()
    )


def newest_unlisting_is_self() -> ColumnElement[bool]:
    """**The one definition of "the mentor took themselves off".**

    The newest unlisting says `mentor_paused` **and** was written by the mentor —
    or by the migration, which acted for nobody (`created_by` NULL) and carried
    legacy self-pauses. The actor matters, not just the spelling: an admin's
    reason is free text, and an admin who typed `mentor_paused` must not hand the
    mentor the resume button. Correlated to `MentorProfile`; it is what resume,
    the pause guard and `paused_by_mentor` all read (#75).
    """
    event = aliased(MentorStatusEvent)
    return exists(
        select(event.id)
        .where(
            event.id == _newest_unlisting_id(),
            event.reason == UnlistedReason.MENTOR_PAUSED.value,
            or_(event.created_by.is_(None), event.created_by == event.mentor_user_id),
        )
        .correlate(MentorProfile)
    )


def paused_by_mentor() -> ColumnElement[bool]:
    """Currently unlisted, and by their own pause. Correlated to `MentorProfile`."""
    return and_(MentorProfile.listing_status == ListingStatus.UNLISTED, newest_unlisting_is_self())


def reminder_eligible() -> ColumnElement[bool]:
    """**The one definition of "this mentor can still be told to come back".**

    A live account and profile, approved (resume needs approval), still paused
    by themselves — dated or not. Read by the claim and by the send-time
    check alike, so the two cannot drift: they did once, when only one of them
    read `LIVE`. Needs `User` joined to `MentorProfile`.
    """
    return and_(
        LIVE,
        MentorProfile.deleted_at.is_(None),
        MentorProfile.approval_status == ApprovalStatus.APPROVED,
        paused_by_mentor(),
    )


def paused_on() -> Any:
    """The local date the current pause began, correlated to `MentorProfile`.

    **Read from the event log, not stored**: the newest unlisting's time is
    when the pause began, and changing the date of a pause appends no event, so
    the anchor stays put. A `paused_at` column would be a second copy of a fact
    the log already holds (rule 8). In the mentor's current zone. Needs `User`.
    """
    began = (
        select(MentorStatusEvent.created_at)
        .where(MentorStatusEvent.id == _newest_unlisting_id())
        .correlate(MentorProfile)
        .scalar_subquery()
    )
    return func.date(func.timezone(User.timezone, began))


def stage_due(now: dt.datetime, offset: Any) -> ColumnElement[bool]:
    """**SQL's statement of "this stage's morning has come"** — the same moment
    as `domain.listing.reminder_due_at`, pinned to it by a boundary test.

    `RETURN_REMINDER_HOUR` on the stage's day in the mentor's **current** zone:
    `offset` days before the return date when there is one, else `offset` days
    after the pause began. Needs `User` joined.
    """
    local_now = func.timezone(User.timezone, now)
    hour = literal(dt.time(RETURN_REMINDER_HOUR), Time)
    days = func.make_interval(0, 0, 0, offset)
    due = case(
        (MentorProfile.return_on.is_not(None), MentorProfile.return_on + hour - days),
        else_=paused_on() + hour + days,
    )
    return local_now >= due


def return_reminder_due(now: dt.datetime) -> ColumnElement[bool]:
    """The pending stage's morning has come — what the claim selects on."""
    return stage_due(now, MentorProfile.return_reminder_stage)


def _after_latest(now: dt.datetime, *, dated: bool) -> Any:
    """The stage pending once the latest due one is sent — checked from the
    last stage back, so a missed stage is never sent late."""
    stages = cadence(dated=dated)
    return case(
        *[
            (stage_due(now, literal(offset, Integer)), literal(stage_after(offset, dated=dated)))
            for offset in reversed(stages)
        ],
        else_=MentorProfile.return_reminder_stage,
    )


def pending_after_claim(now: dt.datetime) -> Any:
    """What the claim sets the pending stage to: past the latest due stage."""
    return case(
        (MentorProfile.return_on.is_not(None), _after_latest(now, dated=True)),
        else_=_after_latest(now, dated=False),
    )


#: What a queued return reminder should do now.
ReminderState = Literal["due", "wait", "stale"]


async def return_reminder_state(
    session: AsyncSession, user_id: UUID, payload: dict[str, Any], now: dt.datetime
) -> ReminderState:
    """Whether a queued return reminder should be sent, kept, or dropped.

    Checked **at send time**: a reminder claimed and queued can wait for a
    retry, and by then the mentor may have resumed, been unlisted by an admin,
    set a new date, or moved to another zone.

    * `stale`: no longer approved, no longer paused by themselves, a different
      date (or none) than the one queued, that stage re-armed by a new pause,
      or a later stage already due. The message would be false, twice, or
      count the wrong number of days.
    * `wait`: still true, but not yet the return morning in their current
      zone. Left pending for a later run rather than sent a day early.
    * `due`: send it.

    **Not locked, deliberately.** A resume committing in the instant between
    this check and the send can still let one reminder out: closing that would
    mean holding the profile row across the email provider's HTTP call, which
    blocks every transition on the mentor for the whole drain (#226).
    """
    queued_for, stage = payload.get("return_on"), payload.get("stage")
    if queued_for is None or stage is None:
        return "stale"
    offset, dated = int(stage), bool(queued_for)
    row = (
        await session.execute(
            select(
                func.timezone(User.timezone, now),
                MentorProfile.return_on,
                paused_on(),
                MentorProfile.return_reminder_stage,
            )
            .select_from(MentorProfile)
            .join(User, User.id == MentorProfile.user_id)
            .where(MentorProfile.user_id == user_id, reminder_eligible())
        )
    ).first()
    if row is None:
        return "stale"
    local_now, return_on, began, pending = row
    # Queued for another pause than this one: a date set, changed or dropped.
    if dated != (return_on is not None):
        return "stale"
    if dated and return_on != dt.date.fromisoformat(str(queued_for)):
        return "stale"
    stages = cadence(dated=dated)
    if offset not in stages:
        return "stale"
    # Claiming moves the pending stage past this one; at or before it means a
    # later pause re-armed it, and the claim will send it again.
    if pending is not None and stages.index(pending) <= stages.index(offset):
        return "stale"
    # A later stage already due: this one would say the wrong count.
    latest = latest_due_stage(return_on=return_on, paused_on=began, local_now=local_now)
    if latest is not None and stages.index(latest) > stages.index(offset):
        return "stale"
    due_at = reminder_due_at(offset, return_on=return_on, paused_on=began)
    return "due" if due_at <= local_now else "wait"
