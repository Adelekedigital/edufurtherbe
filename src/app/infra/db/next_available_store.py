"""When each bookable mentor is next free — refreshed by a job, read by the card.

ADR 0029 allows this one stored derived value, and only because nothing decides
anything with it: booking reads live slots through `list_slots`, and the card
shows `null` whenever it cannot vouch for what is stored.

WHY THE CARD CAN VOUCH FOR IT
=============================
`trg_log_availability_change` appends a `MentorAvailabilityChange` row whenever
anything that decides a mentor's availability changes. A refresh snapshots the
mentor's change rows *before* it computes, then writes its answer and deletes
exactly those rows. The card vouches only while the mentor has no change rows
left and the slot is still bookable.

Review closed two earlier designs. Comparing the refresh's start time against
a `changed_at` blessed a taken slot when a booking committed after the refresh
read the data, and whenever the app host's clock ran ahead of the database's.
Comparing a `changed_at` for equality fixed both but made every booking lock
the mentor's cache row for its whole transaction. A log has neither problem: a
change the snapshot missed is a row nobody deletes, and appends do not block.

**Two runs can overlap** — a QStash retry after a timeout, or the recovery
script beside the schedule. So a write lands only if it was computed later than
the stored value, and a refused write deletes nothing: the older run's answer
cannot replace a newer one, and cannot clear a change it never saw.

WHY THE SLOTS ARE `list_slots`
==============================
The card's time is the first instant `list_slots` offers for any of the
mentor's live offerings, from `mentor_today()` over `MAX_PROJECTION_DAYS` —
exactly the range `/slots` answers. `bookable_until` is that slot's start less
the offering's notice: past it `/slots` no longer offers the slot, so the card
stops showing it rather than promise a time booking refuses.

**The job's calendar reader does not fail open.** `/slots` answers one request
from declared hours when Google is down; stored, that answer would be shown to
everyone for a cycle. So a failed read raises, the mentor is skipped, and
whatever the card showed before stands.

LOCKS, AND WHAT A FAILURE COSTS
===============================
Each mentor is computed, then written and committed on its own, with the clock
read per mentor. Computing takes **no row locks**, so a booking never waits on
a Google call. It does hold the ordinary read locks of an open transaction for
up to one Google timeout, which only DDL would notice — and migrations here set
`lock_timeout` and are retried. One mentor failing is rolled back, logged and
counted; the rest of the run carries on.

**Known limit:** `list_slots` re-reads the mentor's rules and bookings once per
offering, and mentors are computed one after another. At today's few dozen that
is seconds. Bounded concurrency and one load per mentor are the next step if a
run approaches the manifest's timeout.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import case, delete, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.availability import MAX_PROJECTION_DAYS, UtcInterval
from app.infra.db.models.availability import MentorAvailabilityChange, MentorNextAvailability
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.user import User
from app.infra.db.public_visibility import bookable_mentors
from app.infra.db.session_type_store import list_session_types
from app.infra.db.slot_store import FreeBusyReader, list_slots, mentor_today

__all__ = ["next_available_state", "refresh_next_available"]

logger = logging.getLogger(__name__)

OPEN = "open"
NONE = "none"
REFRESHING = "refreshing"

#: How early a run may arrive and still count a value as due. QStash delivers a
#: few seconds either side of the cron, so a maximum age equal to the interval
#: would otherwise recompute on some runs and skip others. With this, the
#: default of five minutes on a five-minute cron means *every run*.
JITTER = dt.timedelta(seconds=60)


def _unchanged() -> Any:
    """A value was computed and nothing has changed for this mentor since."""
    changed = exists().where(
        MentorAvailabilityChange.mentor_user_id == MentorNextAvailability.mentor_user_id
    )
    return MentorNextAvailability.id.is_not(None) & ~changed


def next_available_state() -> Any:
    """`open`, `none` or `refreshing` — the three things a null time can hide.

    **One `CASE`, so the change log is probed once per card.** The time itself
    is shown only when this says `open`; the schema applies that, so the card
    never evaluates the vouching twice.

    A slot whose booking window has closed reads as `refreshing`, not `none`:
    something *was* free, and the next run will say what is free now.
    """
    return case(
        (~_unchanged(), REFRESHING),
        (MentorNextAvailability.bookable_until > func.now(), OPEN),
        (MentorNextAvailability.next_available_at.is_(None), NONE),
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


async def _save(
    session: AsyncSession,
    mentor: UUID,
    *,
    first: dt.datetime | None,
    until: dt.datetime | None,
    computed_at: dt.datetime,
    seen: list[UUID],
) -> bool:
    """Write one mentor's answer unless a later one is already stored.

    Returns whether it was written. A refused write deletes no change rows: the
    run that wrote the newer value owns them.
    """
    values = {"next_available_at": first, "bookable_until": until, "computed_at": computed_at}
    written = (
        await session.execute(
            insert(MentorNextAvailability)
            .values(mentor_user_id=mentor, **values)
            .on_conflict_do_update(
                index_elements=["mentor_user_id"],
                set_=values,
                where=MentorNextAvailability.computed_at <= computed_at,
            )
            .returning(MentorNextAvailability.id)
        )
    ).first() is not None
    if written and seen:
        await session.execute(
            delete(MentorAvailabilityChange).where(MentorAvailabilityChange.id.in_(seen))
        )
    return written


async def refresh_next_available(
    session: AsyncSession,
    *,
    max_age: dt.timedelta,
    reader: FreeBusyReader,
    now: dt.datetime | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Recompute and save every bookable mentor that is due.

    Due is never computed, changed since, or older than `max_age` — age is the
    only thing that catches a change made in Google, which fires no trigger.

    `now` pins the clock for a test. Left out, each mentor reads the database's
    clock as it starts, so a mentor computed late in a long run is not computed
    against the run's start.

    **Commits per mentor.** A dry run computes the same set, writes nothing,
    and rolls back; the reader it is given must not commit on its own.
    """
    run_clock = now or (await session.execute(select(func.clock_timestamp()))).scalar_one()
    due_query = (
        select(MentorProfile.user_id, MentorNextAvailability.id.is_(None).label("new"))
        .join(User, User.id == MentorProfile.user_id)
        .outerjoin(
            MentorNextAvailability,
            MentorNextAvailability.mentor_user_id == MentorProfile.user_id,
        )
        .where(MentorProfile.user_id.in_(bookable_mentors()))
        .where(
            or_(
                ~_unchanged(),
                MentorNextAvailability.computed_at <= run_clock - max_age + JITTER,
            )
        )
    )
    due = (await session.execute(due_query)).all()

    counts = {"created": 0, "refreshed": 0, "open": 0, "failed": 0, "superseded": 0}
    for mentor, new in due:
        try:
            moment = now or (await session.execute(select(func.clock_timestamp()))).scalar_one()
            seen = list(
                (
                    await session.execute(
                        select(MentorAvailabilityChange.id).where(
                            MentorAvailabilityChange.mentor_user_id == mentor
                        )
                    )
                ).scalars()
            )
            first, until = await _first_free(session, mentor, now=moment, reader=reader)
            if dry_run:
                written = True
            else:
                # The run's start, not this mentor's: age is checked against
                # the next run's start, so a mentor reached late in a long run
                # would otherwise be skipped by the next run and refreshed only
                # every other time. It also orders overlapping runs.
                written = await _save(
                    session, mentor, first=first, until=until, computed_at=run_clock, seen=seen
                )
                await session.commit()
        except Exception:
            # One mentor's bad timezone or unreachable calendar must not stop
            # the rest; their card keeps whatever it showed.
            logger.exception("next-available refresh failed for mentor %s", mentor)
            await session.rollback()
            counts["failed"] += 1
            continue
        if not written:
            counts["superseded"] += 1
            continue
        counts["created"] += bool(new)
        counts["refreshed"] += 1
        counts["open"] += first is not None

    if dry_run:
        await session.rollback()
    else:
        # A mentor who stops being bookable is never recomputed, so their log
        # would only grow. Dropping it loses nothing: becoming bookable again
        # is itself a change, and logs one.
        await session.execute(
            delete(MentorAvailabilityChange).where(
                MentorAvailabilityChange.mentor_user_id.not_in(bookable_mentors())
            )
        )
        await session.commit()
    return counts
