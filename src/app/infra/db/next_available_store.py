"""When each bookable mentor is next free — refreshed by a job, read by the card.

ADR 0029 allows this one stored derived value, and only because nothing decides
anything with it: booking reads live slots through `list_slots`, and the card
shows `null` whenever it cannot vouch for what is stored.

WHY THE CARD CAN VOUCH FOR IT
=============================
`trg_mark_next_available_stale` sets `changed_at` whenever anything that
decides a mentor's availability changes. A refresh records `computed_at` as the
moment it **started**, and the card shows the value only while
`computed_at >= changed_at` and the time is still ahead. So a booking, an hours
change, or a change that lands *during* a refresh all read as `refreshing` until
the next run. Recording when the refresh finished instead would bless a value
computed before a change it never saw.

WHY THE SLOTS ARE `list_slots`
==============================
The card's time is the first instant `list_slots` offers for any of the
mentor's live offerings, over `MAX_PROJECTION_DAYS`. A second slot computation
here would be the copy that drifts, and the card would promise a time the
booking flow refuses.

THREE PHASES, AND WHY NO LOCK SPANS A GOOGLE CALL
=================================================
Rows for newly bookable mentors are created and committed first. Computing then
only reads, including one free/busy call per mentor. The writes come last, as
one short transaction the caller commits. Holding a row lock across the compute
phase would make a mentee's booking — whose trigger updates the same row — wait
on Google.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, and_, case, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.availability import MAX_PROJECTION_DAYS, UtcInterval
from app.infra.db.models.availability import MentorNextAvailability
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.user import User
from app.infra.db.public_visibility import mentor_is_bookable, mentor_is_public
from app.infra.db.session_type_store import list_session_types
from app.infra.db.slot_store import FreeBusyReader, list_slots

__all__ = ["next_available_at", "next_available_state", "refresh_next_available"]

OPEN = "open"
NONE = "none"
REFRESHING = "refreshing"


def _vouched() -> Any:
    """The stored value may be shown: computed, and since the last change."""
    return and_(
        MentorNextAvailability.computed_at.is_not(None),
        MentorNextAvailability.computed_at >= MentorNextAvailability.changed_at,
    )


def next_available_at() -> Any:
    """The card's time: the stored value when it is vouched for and still ahead."""
    return case(
        (
            and_(_vouched(), MentorNextAvailability.next_available_at > func.now()),
            MentorNextAvailability.next_available_at,
        ),
        else_=None,
    )


def next_available_state() -> Any:
    """`open`, `none` or `refreshing` — the three things a null can hide.

    A time that has passed reads as `refreshing`, not `none`: something *was*
    free, and the next run will say what is free now.
    """
    return case(
        (
            and_(_vouched(), MentorNextAvailability.next_available_at > func.now()),
            OPEN,
        ),
        (and_(_vouched(), MentorNextAvailability.next_available_at.is_(None)), NONE),
        else_=REFRESHING,
    )


class _OneReadPerMentor:
    """A `FreeBusyReader` that asks the real one once per mentor and range.

    Every offering of one mentor asks for the same range, so without this a
    mentor with three offerings would cost three Google calls a refresh.
    """

    def __init__(self, reader: FreeBusyReader) -> None:
        self.reader = reader
        self.seen: dict[tuple[UUID, dt.datetime, dt.datetime], tuple[UtcInterval, ...]] = {}

    async def busy(
        self, session: AsyncSession, user_id: UUID, start: dt.datetime, end: dt.datetime
    ) -> tuple[UtcInterval, ...]:
        key = (user_id, start, end)
        if key not in self.seen:
            self.seen[key] = await self.reader.busy(session, user_id, start, end)
        return self.seen[key]


async def _first_free(
    session: AsyncSession, mentor: UUID, *, now: dt.datetime, reader: FreeBusyReader
) -> dt.datetime | None:
    """The earliest instant any of this mentor's offerings could be booked."""
    offerings = await list_session_types(session, mentor) or []
    # Yesterday in UTC, so the mentor's own "today" is inside the range whatever
    # their zone; a day already past yields no slots, so starting early is free.
    start = (now - dt.timedelta(days=1)).date()
    end = start + dt.timedelta(days=MAX_PROJECTION_DAYS)
    once = _OneReadPerMentor(reader)
    firsts = []
    for offering in offerings:
        slots = await list_slots(
            session, mentor, offering["id"], start=start, end=end, now=now, external_busy=once
        )
        if slots:
            firsts.append(min(slot.start for slot in slots))
    return min(firsts, default=None)


async def refresh_next_available(
    session: AsyncSession,
    *,
    now: dt.datetime,
    max_age: dt.timedelta,
    reader: FreeBusyReader,
) -> dict[str, int]:
    """Create rows for new mentors, then recompute every row that is due.

    Due is stale (changed since computed, or never computed) or older than
    `max_age` — age is the only thing that catches a change made in Google,
    which fires no trigger here.

    **Commits once, after creating rows**, so they are visible to the triggers
    before anything is computed for them. A dry run therefore still creates
    empty rows; they carry no value and the next run would create them anyway.
    The updates are left for the caller to commit or roll back.
    """
    bookable = (
        select(MentorProfile.user_id)
        .join(User, User.id == MentorProfile.user_id)
        .where(*mentor_is_public(), *mentor_is_bookable())
    )
    # `changed_at` at the epoch: a row just created has seen no change yet.
    # Its insert time would be *after* this run's `now`, and the first value
    # computed for it would read as stale forever — every refresh would record
    # a `computed_at` earlier than the row's own birth.
    never = func.to_timestamp(0)
    created = await session.execute(
        insert(MentorNextAvailability)
        .from_select(["mentor_user_id", "changed_at"], bookable.add_columns(never))
        .on_conflict_do_nothing(index_elements=["mentor_user_id"])
    )
    await session.commit()

    due = (
        (
            await session.execute(
                select(MentorNextAvailability.mentor_user_id).where(
                    MentorNextAvailability.mentor_user_id.in_(bookable),
                    or_(
                        ~_vouched(),
                        MentorNextAvailability.computed_at <= now - max_age,
                    ),
                )
            )
        )
        .scalars()
        .all()
    )

    results = {mentor: await _first_free(session, mentor, now=now, reader=reader) for mentor in due}

    for mentor, first in results.items():
        await session.execute(
            update(MentorNextAvailability)
            .where(MentorNextAvailability.mentor_user_id == mentor)
            .values(next_available_at=first, computed_at=now)
        )
    return {
        "created": cast("CursorResult[Any]", created).rowcount or 0,
        "refreshed": len(results),
        "open": sum(1 for first in results.values() if first is not None),
    }
