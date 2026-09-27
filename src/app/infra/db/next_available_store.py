"""When each bookable mentor is next free — refreshed by a job, read by the card.

ADR 0029 allows this one stored derived value, and only because nothing decides
anything with it: booking reads live slots through `list_slots`, and the card
shows `null` whenever it cannot vouch for what is stored.

WHY THE CARD CAN VOUCH FOR IT
=============================
`trg_mark_next_available_stale` sets a mentor's `changed_at` whenever anything
that decides their availability changes. A refresh reads `changed_at` *before*
it computes, and writes that value back as `seen_changed_at` beside the result.
The card shows the result only while `seen_changed_at = changed_at` and the time
is still ahead.

**An equality, deliberately, not a comparison of two clocks.** The first version
compared the job's start time against `changed_at`, and review found two ways it
blessed a taken slot: a booking whose trigger fired before the refresh started
but whose transaction committed after the refresh read `sessions`, and an app
host whose clock ran ahead of the database's. Both break an equality. The first
because the refresh read the *old* `changed_at` (the booking's was uncommitted),
so the commit leaves them unequal; the second because no clock is compared.

WHY THE SLOTS ARE `list_slots`
==============================
The card's time is the first instant `list_slots` offers for any of the
mentor's live offerings, from the mentor's own today over `MAX_PROJECTION_DAYS` —
exactly the range `/slots` will answer. A second slot computation here would be
the copy that drifts, and the card would promise a time booking refuses.

NO LOCK SPANS A GOOGLE CALL, AND A TIMEOUT KEEPS WHAT IT FINISHED
================================================================
Each mentor is computed, then written and committed on its own. Computing only
reads, so a mentee's booking — whose trigger writes the same row — waits at most
for one `UPDATE`, never for Google. And a run QStash times out has still saved
every mentor it reached.

**Known limit:** `list_slots` re-reads the mentor's rules and bookings once per
offering, and mentors are computed one after another. At today's few dozen that
is seconds. Bounded concurrency and one load per mentor are the next step if a
run approaches the manifest's timeout.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, cast
from uuid import UUID
from zoneinfo import ZoneInfo

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

#: How early a run may arrive and still count a value as due. QStash delivers a
#: few seconds either side of the cron, so a maximum age equal to the interval
#: would otherwise recompute on some runs and skip others. With this, the
#: default of five minutes on a five-minute cron means *every run*.
JITTER = dt.timedelta(seconds=60)


def _vouched() -> Any:
    """The stored value was computed with no change since."""
    return and_(
        MentorNextAvailability.seen_changed_at.is_not(None),
        MentorNextAvailability.seen_changed_at == MentorNextAvailability.changed_at,
    )


def _ahead() -> Any:
    return MentorNextAvailability.next_available_at > func.now()


def next_available_at() -> Any:
    """The card's time: the stored value when it is vouched for and still ahead."""
    return case((and_(_vouched(), _ahead()), MentorNextAvailability.next_available_at), else_=None)


def next_available_state() -> Any:
    """`open`, `none` or `refreshing` — the three things a null can hide.

    A time that has passed reads as `refreshing`, not `none`: something *was*
    free, and the next run will say what is free now.
    """
    return case(
        (and_(_vouched(), _ahead()), OPEN),
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
    zone = (await session.execute(select(User.timezone).where(User.id == mentor))).scalar_one()
    # The mentor's today, which is how `list_slots` defaults its own range, so
    # the whole horizon a mentee could ask `/slots` about is searched here.
    start = now.astimezone(ZoneInfo(zone)).date()
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
    dry_run: bool = False,
) -> dict[str, int]:
    """Create rows for new mentors, then recompute and save every row that is due.

    Due is not vouched for (changed since computed, or never computed) or older
    than `max_age` — age is the only thing that catches a change made in Google,
    which fires no trigger here.

    **Commits as it goes**: the new rows, then each mentor as it is computed. A
    dry run creates and writes nothing, and reports what it would have done.
    """
    bookable = (
        select(MentorProfile.user_id)
        .join(User, User.id == MentorProfile.user_id)
        .where(*mentor_is_public(), *mentor_is_bookable())
    )
    created = 0
    if not dry_run:
        result = await session.execute(
            insert(MentorNextAvailability)
            .from_select(["mentor_user_id"], bookable)
            .on_conflict_do_nothing(index_elements=["mentor_user_id"])
        )
        created = cast("CursorResult[Any]", result).rowcount or 0
        await session.commit()

    due = (
        await session.execute(
            select(MentorNextAvailability.mentor_user_id, MentorNextAvailability.changed_at).where(
                MentorNextAvailability.mentor_user_id.in_(bookable),
                or_(
                    ~_vouched(),
                    MentorNextAvailability.computed_at <= now - max_age + JITTER,
                ),
            )
        )
    ).all()

    refreshed = found = 0
    for mentor, seen in due:
        first = await _first_free(session, mentor, now=now, reader=reader)
        refreshed += 1
        found += first is not None
        if dry_run:
            continue
        await session.execute(
            update(MentorNextAvailability)
            .where(MentorNextAvailability.mentor_user_id == mentor)
            .values(next_available_at=first, computed_at=now, seen_changed_at=seen)
        )
        await session.commit()
    if dry_run:
        await session.rollback()
    return {"created": created, "refreshed": refreshed, "open": found}
