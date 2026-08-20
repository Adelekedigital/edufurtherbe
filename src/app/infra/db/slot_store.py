"""Bookable slots for one mentor's session type, computed on demand.

**Nothing here is stored.** Package D18 settles it: availability is rules and
exceptions, and slots are derived. A stored slot table is the drift that made
the legacy front-search table untrustworthy, and it would be wrong the moment a
session is booked.

**The visibility rule is one statement, and it is the first public one in this
codebase.** Every other read scopes to a viewer; this one has no viewer at all,
so what stands in its place is the mentor's own state:

    approval_status = 'approved' AND listing_status = 'listed'

Both halves are load-bearing and the pair is **not** redundant. `apply_mentor_status`
writes one or the other and never both — that separation is deliberate, so one
event can state one fact — and no CHECK ties them. A `pending` mentor who is
`listed` is therefore a legal row, and gating on listing alone would publish an
unvetted mentor's calendar to anyone who asked.

**Two kinds of busy, subtracted as one.** Sessions this platform booked come
from the database; a connected mentor's other commitments come from Google, read
at the moment of asking and never stored (ADR 0004). By the time `bookable` sees
them they are both just intervals, which is deliberate — a commitment is a
commitment regardless of who knew about it first.

The Google half is **advisory and fails open**: unreachable means it contributed
nothing and the grid is whatever the mentor declared, which is exactly what every
mentor without a connection gets. `sessions_no_mentor_double_booking` remains the
thing that actually prevents a double booking; this only stops one being offered.

**A session's window comes from `session_window()`, not from Python.** The same
function backs `sessions_no_mentor_double_booking`, so the slots this returns
and the constraint that would refuse a booking are computing from one
definition. `starts_at + timedelta(...)` in Python would be a second copy of a
rule the database already owns, and the copy that drifts silently offers a slot
that is taken.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationError
from app.domain.availability import (
    DEFAULT_PROJECTION_DAYS,
    MAX_PROJECTION_DAYS,
    DatedException,
    UtcInterval,
    WeeklyWindow,
    bookable,
)
from app.domain.enums import AvailabilityExceptionType
from app.infra.db.calendar_store import NullFreeBusy
from app.infra.db.models.availability import (
    AvailabilityException,
    AvailabilityRule,
    SessionTypeSchedulingWindow,
)
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.sessions import (
    FREES_THE_HOUR,
    Session,
    SessionType,
    SessionTypeBookingConfig,
)
from app.infra.db.models.user import User
from app.infra.db.public_visibility import mentor_is_public, session_type_is_live

__all__ = ["list_slots"]


class FreeBusyReader(Protocol):
    """What `list_slots` needs of a calendar: busy intervals, or nothing.

    Structural rather than inherited, following the room and calendar adapters
    beside it — the null implementation and the real one share no base class and
    do not need one.
    """

    async def busy(
        self, session: AsyncSession, user_id: UUID, start: dt.datetime, end: dt.datetime
    ) -> tuple[UtcInterval, ...]: ...


def _publicly_bookable(user_id: UUID, session_type_id: UUID) -> Select[Any]:
    """The offering, if the public may see it at all.

    Returns nothing — and therefore a 404 — when the mentor is unapproved, is
    unlisted, has no profile, or when the session type belongs to somebody else,
    is inactive, or is soft-deleted. **One response for six reasons**, because
    telling them apart tells anyone who can guess an id which mentors exist and
    what state they are in.

    ``SessionType.mentor_user_id == user_id`` is not decoration: without it a
    caller could read any mentor's session type through any mentor's URL, and
    the duration it returned would be the wrong offering's.
    """
    return (
        select(
            SessionTypeBookingConfig.duration_minutes,
            SessionTypeBookingConfig.min_notice_minutes,
            # The mentor's own zone, because `start` and `end` are *their* days.
            # Fetched here rather than in a second statement: a caller who omits
            # `start` needs it to know what "today" means, and this query has
            # already found the mentor.
            User.timezone,
        )
        .select_from(SessionType)
        .join(
            SessionTypeBookingConfig,
            SessionTypeBookingConfig.session_type_id == SessionType.id,
        )
        .join(MentorProfile, MentorProfile.user_id == SessionType.mentor_user_id)
        .join(User, User.id == SessionType.mentor_user_id)
        .where(
            SessionType.id == session_type_id,
            *session_type_is_live(user_id),
            *mentor_is_public(),
        )
    )


def _busy(user_id: UUID, span_start: dt.datetime, span_end: dt.datetime) -> Select[Any]:
    """Every session still occupying the mentor's time over the span.

    **`FREES_THE_HOUR` is subtracted, and the test is whether the session is
    still happening.** A `missed` one cannot arise here at all: its start has
    passed, and `bookable` never offers a slot before `now`.

    **Every status subtracted here is also absent from `LIVE_STATUSES`**, and
    keeping those two in step is the whole job of that constant. The exclusion
    constraint is built over `LIVE_STATUSES`, so a status this query hides but
    the constraint ignores is an hour nobody can book and nothing is protecting
    — hidden from the grid, unguarded in the database, and never coming back.

    That has been a live defect twice. `declined`, `withdrawn` and `expired`
    were the first: a mentee could request every slot, withdraw, and empty a
    mentor's calendar permanently. `cancelled` was the second and survived
    longer, because it looked deliberate — this docstring used to say it "keeps
    its slot until somebody deliberately releases it", and nothing ever
    released it.

    **A mentor who is not free says so with an availability exception**, which
    a cancellation writes when they answer that they are not. It is theirs to
    see and to remove, it applies to every offering, and it is subtracted by
    `bookable` further down rather than here.

    The overlap test is `&&` against `session_window()` rather than a comparison
    on `starts_at`, so a session that began before the span but runs into it is
    caught. Comparing starts alone would miss it, and the slot it occupies would
    be offered.
    """
    window = func.session_window(Session.starts_at, Session.duration_minutes)
    return select(
        func.lower(window).label("start"),
        func.upper(window).label("end"),
    ).where(
        Session.mentor_id == user_id,
        Session.status.notin_(FREES_THE_HOUR),
        window.op("&&")(func.tstzrange(span_start, span_end)),
    )


async def list_slots(
    session: AsyncSession,
    user_id: UUID,
    session_type_id: UUID,
    *,
    start: dt.date | None,
    end: dt.date | None,
    now: dt.datetime,
    external_busy: FreeBusyReader | None = None,
) -> list[UtcInterval] | None:
    """Slots someone could book with this mentor, or ``None`` if they may not look.

    ``None`` means "no such publicly bookable offering" and becomes a 404. An
    **empty list** means the offering exists and has nothing free, which is a
    different and true statement — collapsing the two would tell a caller that a
    fully booked mentor does not exist.

    ``now`` is passed in rather than read here. The notice window makes this
    answer depend on the clock, and a function that reads its own clock cannot be
    tested against a DST boundary without moving the machine's timezone.

    **The dates default here rather than at the edge, because the default needs
    the mentor.** An omitted ``start`` means today — and today is a question with
    no answer until a timezone is named. It cannot be the caller's: this endpoint
    takes no token, so there is no profile to read one from, and IP or
    ``Accept-Language`` are guesses that are wrong for anyone travelling. It is
    the **mentor's**, which is also how a supplied ``start`` is already read.

    Using UTC instead would look equivalent and quietly lose slots. A mentor in
    New York at 02:00 UTC is at 21:00 the previous day; their evening window is
    still ahead of them, and a UTC "today" would skip it. Being wrong the other
    way costs nothing, because a day already past yields no slots anyway.
    """
    external_busy = external_busy or NullFreeBusy()
    offering = (
        (await session.execute(_publicly_bookable(user_id, session_type_id))).mappings().first()
    )
    if offering is None:
        return None

    if start is None:
        start = now.astimezone(ZoneInfo(offering["timezone"])).date()
    if end is None:
        end = start + dt.timedelta(days=DEFAULT_PROJECTION_DAYS)

    # Validated *after* defaulting, not before. A caller may send `end` alone,
    # and whether that range is legal depends on the `start` we just chose — so
    # checking at the edge would check a value the request did not have.
    if end <= start:
        raise ValidationError("`end` must be after `start`")
    if (end - start).days > MAX_PROJECTION_DAYS:
        raise ValidationError(f"a range may span at most {MAX_PROJECTION_DAYS} days")

    # **The offering's own windows replace general availability; they do not
    # intersect it.** Asked first, and only if it has none does the mentor's
    # `availability_rules` answer — which is what every offering did before
    # windows existed, and is why an offering with none produces byte-identical
    # slots to before.
    #
    # Intersecting was the obvious reading and the mock's own example shows why
    # it is wrong: deliberate evening and morning windows, intersected with
    # normal working hours, yield zero slots and an empty calendar with nothing
    # to explain it.
    #
    # `exceptions` below are **not** switched. Windows replace *availability*,
    # not *unavailability*: a mentor who blocked a date blocked it for every
    # offering.
    rules = (
        await session.execute(
            select(
                SessionTypeSchedulingWindow.day_of_week,
                SessionTypeSchedulingWindow.start_time,
                SessionTypeSchedulingWindow.end_time,
                SessionTypeSchedulingWindow.timezone,
            ).where(
                SessionTypeSchedulingWindow.session_type_id == session_type_id,
                SessionTypeSchedulingWindow.is_active.is_(True),
                SessionTypeSchedulingWindow.deleted_at.is_(None),
            )
        )
    ).mappings()
    windows = [dict(row) for row in rules]

    if not windows:
        windows = [
            dict(row)
            for row in (
                await session.execute(
                    select(
                        AvailabilityRule.day_of_week,
                        AvailabilityRule.start_time,
                        AvailabilityRule.end_time,
                        AvailabilityRule.timezone,
                    ).where(
                        AvailabilityRule.mentor_user_id == user_id,
                        AvailabilityRule.is_active.is_(True),
                        AvailabilityRule.deleted_at.is_(None),
                    )
                )
            ).mappings()
        ]

    exceptions = (
        await session.execute(
            select(
                AvailabilityException.type,
                func.lower(AvailabilityException.date_range).label("start_date"),
                func.upper(AvailabilityException.date_range).label("end_date"),
                AvailabilityException.start_time,
                AvailabilityException.end_time,
                AvailabilityException.timezone,
            ).where(
                AvailabilityException.mentor_user_id == user_id,
                AvailabilityException.deleted_at.is_(None),
            )
        )
    ).mappings()

    # The span the sessions query asks about is the whole requested range as
    # instants. Days are the mentor's, but a session is an instant either way,
    # and asking one day wide on each side would only add rows `bookable`
    # discards.
    span_start = dt.datetime.combine(start, dt.time.min, tzinfo=dt.UTC) - dt.timedelta(days=1)
    span_end = dt.datetime.combine(end, dt.time.min, tzinfo=dt.UTC) + dt.timedelta(days=1)
    busy = (await session.execute(_busy(user_id, span_start, span_end))).mappings()

    # **The mentor's own calendar, subtracted over the same span.** One request
    # for the whole range rather than one per day, and none at all unless this
    # mentor has connected — which most have not.
    #
    # It arrives as intervals because that is all `bookable` accepts and all the
    # scope grants, so the two halves of `busy` are indistinguishable by the
    # time they are subtracted. That is the point: a commitment is a commitment
    # whether we booked it or Google knows about it.
    #
    # **Advisory, and it fails open** (ADR 0004). `external_busy` returns `()`
    # when Google cannot be reached, so an outage degrades this to declared
    # availability alone rather than emptying every connected mentor's calendar.
    # The exclusion constraint remains the thing that actually prevents a double
    # booking; this only stops one being offered.
    elsewhere = await external_busy.busy(session, user_id, span_start, span_end)

    return list(
        bookable(
            rules=[
                WeeklyWindow(
                    day_of_week=row["day_of_week"],
                    start_time=row["start_time"],
                    end_time=row["end_time"],
                    timezone=row["timezone"],
                )
                for row in windows
            ],
            exceptions=[
                DatedException(
                    kind=AvailabilityExceptionType(row["type"]),
                    start_date=row["start_date"],
                    end_date=row["end_date"],
                    timezone=row["timezone"],
                    start_time=row["start_time"],
                    end_time=row["end_time"],
                )
                for row in exceptions
            ],
            busy=[UtcInterval(row["start"], row["end"]) for row in busy] + list(elsewhere),
            duration_minutes=offering["duration_minutes"],
            min_notice_minutes=offering["min_notice_minutes"],
            now=now,
            start=start,
            end=end,
        )
    )
