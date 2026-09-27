"""When each bookable mentor is next free — refreshed by a job, read by the card.

ADR 0029 allows this one stored derived value, and only because nothing decides
anything with it: booking reads live slots through `list_slots`, and the card
shows `null` whenever it cannot vouch for what is stored.

WHY THE CARD CAN VOUCH FOR IT
=============================
`trg_log_availability_change` appends a `MentorAvailabilityChange` row whenever
anything that decides a mentor's availability changes. A refresh snapshots the
mentor's change rows *before* it computes, then writes its answer and deletes
exactly those rows. The card shows the answer only while the mentor has no
change rows left and the slot is still bookable.

Review closed two earlier designs. Comparing the refresh's start time against
a `changed_at` blessed a taken slot when a booking committed after the refresh
read the data, and whenever the app host's clock ran ahead of the database's.
Comparing a `changed_at` for equality fixed both but made every booking lock
the mentor's cache row for its whole transaction. A log has neither problem: a
change the snapshot missed is a row nobody deletes, and appends do not block.

WHY THE SLOTS ARE `list_slots`
==============================
The card's time is the first instant `list_slots` offers for any of the
mentor's live offerings, from `mentor_today()` over `MAX_PROJECTION_DAYS` —
exactly the range `/slots` answers. `bookable_until` is that slot's start less
the offering's notice: past it `/slots` no longer offers the slot, so the card
stops showing it rather than promise a time booking refuses.

NO LOCK SPANS A GOOGLE CALL, AND A TIMEOUT KEEPS WHAT IT FINISHED
================================================================
Each mentor is computed, then written and committed on its own, and computing
only reads. A run QStash times out has still saved every mentor it reached.

**Known limit:** `list_slots` re-reads the mentor's rules and bookings once per
offering, and mentors are computed one after another. At today's few dozen that
is seconds. Bounded concurrency and one load per mentor are the next step if a
run approaches the manifest's timeout.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import and_, case, delete, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.availability import MAX_PROJECTION_DAYS, UtcInterval
from app.infra.db.models.availability import MentorAvailabilityChange, MentorNextAvailability
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.user import User
from app.infra.db.public_visibility import mentor_is_bookable, mentor_is_public
from app.infra.db.session_type_store import list_session_types
from app.infra.db.slot_store import FreeBusyReader, list_slots, mentor_today

__all__ = ["next_available_at", "next_available_state", "refresh_next_available"]

OPEN = "open"
NONE = "none"
REFRESHING = "refreshing"

#: How early a run may arrive and still count a value as due. QStash delivers a
#: few seconds either side of the cron, so a maximum age equal to the interval
#: would otherwise recompute on some runs and skip others. With this, the
#: default of five minutes on a five-minute cron means *every run*.
JITTER = dt.timedelta(seconds=60)


def _vouched() -> Any:
    """A value was computed and nothing has changed for this mentor since."""
    changed = exists().where(
        MentorAvailabilityChange.mentor_user_id == MentorNextAvailability.mentor_user_id
    )
    return and_(MentorNextAvailability.id.is_not(None), ~changed)


def _bookable_now() -> Any:
    return MentorNextAvailability.bookable_until > func.now()


def next_available_at() -> Any:
    """The card's time: the stored value when it is vouched for and still bookable."""
    return case(
        (and_(_vouched(), _bookable_now()), MentorNextAvailability.next_available_at),
        else_=None,
    )


def next_available_state() -> Any:
    """`open`, `none` or `refreshing` — the three things a null can hide.

    A slot whose booking window has closed reads as `refreshing`, not `none`:
    something *was* free, and the next run will say what is free now.
    """
    return case(
        (and_(_vouched(), _bookable_now()), OPEN),
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
) -> tuple[dt.datetime | None, dt.datetime | None]:
    """The earliest instant any offering could be booked, and until when."""
    offerings = await list_session_types(session, mentor) or []
    zone = (await session.execute(select(User.timezone).where(User.id == mentor))).scalar_one()
    start = mentor_today(zone, now)
    end = start + dt.timedelta(days=MAX_PROJECTION_DAYS)
    once = _OneReadPerMentor(reader)
    best: tuple[dt.datetime, dt.datetime] | None = None
    for offering in offerings:
        slots = await list_slots(
            session, mentor, offering["id"], start=start, end=end, now=now, external_busy=once
        )
        if not slots:
            continue
        first = min(slot.start for slot in slots)
        until = first - dt.timedelta(minutes=int(offering["min_notice_minutes"]))
        # The same start from two offerings stays bookable while either takes it.
        if best is None or first < best[0] or (first == best[0] and until > best[1]):
            best = (first, until)
    return best if best is not None else (None, None)


async def refresh_next_available(
    session: AsyncSession,
    *,
    now: dt.datetime,
    max_age: dt.timedelta,
    reader: FreeBusyReader,
    dry_run: bool = False,
) -> dict[str, int]:
    """Recompute and save every bookable mentor that is due.

    Due is never computed, changed since, or older than `max_age` — age is the
    only thing that catches a change made in Google, which fires no trigger.

    **Commits per mentor.** A dry run computes the same set, writes nothing,
    and rolls back; the reader it is given must not commit on its own (the
    runner passes one without a session factory).
    """
    visible = (MentorProfile.user_id,)
    due_query = (
        select(*visible, MentorNextAvailability.id.is_(None).label("new"))
        .join(User, User.id == MentorProfile.user_id)
        .outerjoin(
            MentorNextAvailability,
            MentorNextAvailability.mentor_user_id == MentorProfile.user_id,
        )
        .where(*mentor_is_public(), *mentor_is_bookable())
        .where(
            or_(
                ~_vouched(),
                MentorNextAvailability.computed_at <= now - max_age + JITTER,
            )
        )
    )
    due = (await session.execute(due_query)).all()

    created = refreshed = found = 0
    for mentor, new in due:
        seen = (
            (
                await session.execute(
                    select(MentorAvailabilityChange.id).where(
                        MentorAvailabilityChange.mentor_user_id == mentor
                    )
                )
            )
            .scalars()
            .all()
        )
        first, until = await _first_free(session, mentor, now=now, reader=reader)
        created += bool(new)
        refreshed += 1
        found += first is not None
        if dry_run:
            continue
        values = {"next_available_at": first, "bookable_until": until, "computed_at": now}
        await session.execute(
            insert(MentorNextAvailability)
            .values(mentor_user_id=mentor, **values)
            .on_conflict_do_update(index_elements=["mentor_user_id"], set_=values)
        )
        if seen:
            await session.execute(
                delete(MentorAvailabilityChange).where(MentorAvailabilityChange.id.in_(seen))
            )
        await session.commit()

    if dry_run:
        await session.rollback()
    else:
        # A mentor who stops being bookable is never recomputed, so their log
        # would only grow. Dropping it loses nothing: becoming bookable again
        # is itself a change, and logs one.
        bookable = (
            select(MentorProfile.user_id)
            .join(User, User.id == MentorProfile.user_id)
            .where(*mentor_is_public(), *mentor_is_bookable())
        )
        await session.execute(
            delete(MentorAvailabilityChange).where(
                MentorAvailabilityChange.mentor_user_id.not_in(bookable)
            )
        )
        await session.commit()
    return {"created": created, "refreshed": refreshed, "open": found}
