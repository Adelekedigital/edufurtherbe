"""What a mentor has actually done, derived every time it is asked — and the
one mentee-side count, `received()`, which reads the same `_completed()` clause
so the two sides cannot disagree about what *completed* means.

**Nothing here is stored, and D56 is why**: *"counts and ratios are derived at
query time, never stored — no cached totals, no denormalised counters, no
percentage columns."* The migration package agrees from the other direction: it
lists `countCompletedSession` and `percentageOfCompletedSession` on *Mentor
(front search)* — the discovery card — and drops both as "DERIVED at query time".

ONE DEFINITION OF DELIVERED, TWO READERS
========================================
The discovery card shows a completed-session count and so does the profile: the
same number about the same mentor. So it is one predicate, `delivered()`, and
both import it. It takes the mentor as a parameter because the two callers
identify them differently — the card correlates on `MentorProfile.user_id` inside
a paged query, the profile passes a resolved id — and that difference is the only
thing that ever tempted anyone to write it twice.

WHAT `completed` IS SUPPOSED TO MEAN, AND WHAT IT ACTUALLY MEANS
================================================================
`completed` is *intended* to mean both parties turned up. **In the migrated data
it does not**, and that was found by loading the export rather than by reading
it. Counted across the loaded database:

    completed  mentor attended  34     completed  mentee attended  35
    completed  mentor no_show    2     completed  mentee no_show    1

Three sessions are `completed` with somebody absent. The first version of this
docstring claimed perfect agreement, measured across the 103 linked
booking-tracker pairs — but most sessions come from the **164 orphan trackers**,
which take a different path entirely: `status = CANCELLED if cancelled else
COMPLETED`, which never consults attendance. "Not cancelled" became "completed".

**Nothing enforces the invariant and the migrated data already violates it.**
`sessions.status` and `session_participants.attendance_status` are independent
columns, and a `CHECK` cannot tie them because it spans two tables.

Every stat here therefore answers from the column it is *about* — the session
says delivered, so it counts; the participant says absent, so the rate falls —
and a reader seeing "11 sessions, 69%" is looking at a contradiction that is
really in the data rather than at a number hiding it.

**A producer now exists, and it applies forward only.**
`session_writer.settle_attendance` derives a session's outcome *from* its
attendance once the join window shuts, so from that point there is one place the
outcome is decided. It settles `confirmed` sessions only, which deliberately
leaves every migrated row untouched: those record what the legacy app believed,
and correcting them would destroy the evidence that the two figures disagree.
So the split above is now historical rather than permanent — post-cutover
sessions agree, migrated ones do not, and this module still reads each column
for what it says.

ATTENDANCE COUNTS TWO STATUSES AND IGNORES TWO
==============================================
The rate is the mentor's own: sessions they attended over sessions they were
expected at.

    numerator     attendance_status = 'attended'
    denominator   attendance_status IN ('attended', 'no_show')

`pending` is *unknown*, not absent — its own docstring says "we do not know yet
is a real answer and distinguishable from `NO_SHOW`" — and it is the state of
every session that has not yet been settled. Counting it as absence would report
a mentor with a session next week as unreliable today.

`left_early` is ignored for a different reason: nothing writes it and it is
slated for removal (`docs/handoff-enum-to-text-check.md`). Enumerating the two
statuses that mean something is what keeps this code from giving meaning to a
label on its way out.

**The denominator is terminal past sessions only** — `completed` and `no_show`.
A cancelled session is not a missed one, and a confirmed session next week has
not happened; either in the denominator reports absence that never occurred.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from sqlalchemy import TIMESTAMP, Integer, Numeric, and_, case, cast, func, literal, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.domain.enums import ActorType, AttendanceStatus, SessionStatus
from app.domain.mentor_stats import Window
from app.infra.db.models.sessions import Session, SessionEvent, SessionParticipant
from app.infra.db.predicates import Bounds, during

__all__ = [
    "ATTENDED",
    "BY_MENTEE",
    "BY_MENTOR",
    "BY_OTHER",
    "EXPECTED",
    "MENTEE",
    "MENTOR",
    "MISSED",
    "TERMINAL",
    "attendance_rate",
    "attendance_sessions",
    "cancellations",
    "canceller",
    "current_streak",
    "delivered",
    "held",
    "held_count",
    "mentee_completed_sessions",
    "mentee_missed",
    "mentee_window_figures",
    "mentor_stats",
    "mentor_window_figures",
    "missed",
    "missed_by",
    "missed_count",
    "received",
]

#: The party turned up: the rate's numerator, and the mentee's side of
#: `mentees_left_waiting` in `_misses()`, so widening it changes both.
ATTENDED = (AttendanceStatus.ATTENDED,)

#: The party was expected and did not come: the half of the rate's
#: denominator that is not `ATTENDED`, which is what "missed" counts.
MISSED = (AttendanceStatus.NO_SHOW,)

#: The party was expected *and* it is known whether they came. Listing what
#: counts is how `pending` and `left_early` stay out without either being named.
EXPECTED = ATTENDED + MISSED

#: Sessions that have happened, or should have. Not `cancelled` — nobody was
#: expected — and not `confirmed`, which has not happened yet.
TERMINAL = (SessionStatus.COMPLETED, SessionStatus.NO_SHOW)

#: Who made a cancellation, as `cancellations()` reads it off the event: the
#: mentor, the mentee, or anybody else (the system, an admin, nobody recorded).
BY_MENTOR = "mentor"
BY_MENTEE = "mentee"
BY_OTHER = "other"


def delivered(mentor: Any) -> Any:
    """The predicate for a session this mentor delivered.

    Scoped to `mentor_id`, because a mentor is also somebody's mentee and a
    session they *received* is not one they gave — the discovery card has a test
    for exactly that inversion.

    Served by `ix_sessions_mentor_completed`, a partial index that existed from
    the M4 schema with no reader until the card.
    """
    return and_(Session.mentor_id == mentor, _completed())


def received(mentee: Any) -> Any:
    """The predicate for a session this mentee received.

    `delivered()` from the other side, and scoped to `mentee_id` for the same
    reason: a dual-role user's sessions as a mentor are not sessions they
    received.
    """
    return and_(Session.mentee_id == mentee, _completed())


def _completed() -> Any:
    """What counts as a session that happened, for either side.

    One clause so the two counts cannot drift — the day `completed` gains a
    sibling status that also means delivered, both sides change together.
    """
    return Session.status == SessionStatus.COMPLETED


async def mentee_completed_sessions(session: AsyncSession, user_id: UUID) -> int:
    """How many sessions `user_id` has received as a mentee.

    Derived per request under D56, like every figure in this module.
    """
    result = await session.execute(
        select(func.count()).select_from(Session).where(received(user_id))
    )
    return int(result.scalar_one())


#: The inner ``sessions`` of the rate subquery, aliased.
#:
#: **Required, not stylistic.** The mentee rate is correlated — its subject comes
#: from the *outer* query's ``sessions.mentee_id`` — so an unaliased inner
#: reference would be the same table twice and PostgreSQL would resolve both to
#: the outer row, quietly returning that one session's attendance as the rate.
_RATE = aliased(Session, name="rate_session")

#: Which side of the session the rate is about. Passed in rather than baked in,
#: for the reason `delivered()` already takes its mentor as a parameter: the two
#: numbers are the same arithmetic over different populations, and two copies is
#: where one of them stops counting `no_show`.
MENTOR = _RATE.mentor_id
MENTEE = _RATE.mentee_id


def attendance_rate(user: Any, side: Any, window: Window | None = None) -> Any:
    """One party's attendance on one side of the table, as a percentage or ``NULL``.

    **The side is load-bearing and the two numbers are not interchangeable.** A
    mentor who is also somebody's mentee has two rates: sessions they *hosted*
    and turned up to, and sessions they *booked* and turned up to. Pooling them
    would let a diligent mentee's record flatter an unreliable mentor, and the
    discovery card already has a test for exactly that inversion on
    `delivered()`.

    A scalar subquery rather than a join, so the ratio is computed where both
    counts are in scope and the outer query stays one row. ``user`` may be a
    concrete id — the profile read has one — or a column of the outer query,
    which is how a session list gets the rate of whichever mentee is on the row.

    **`NULL`, never zero, when nothing is known.** Zero percent says "never shows
    up"; null says "no data yet". Somebody whose sessions have not been settled
    yet must not be branded the first — which is everybody whose only sessions
    are still ahead of them, and every mentee on their first booking.

    ``window`` narrows it to sessions starting inside it; ``None`` is all time.
    """
    came = func.count().filter(SessionParticipant.attendance_status.in_(ATTENDED))
    due = _due()
    # Cast in SQL, not in Python: `round()` returns numeric, so the value
    # arrives as "100.0" and `int()` refuses it. Fixing it at the boundary
    # would leave the column's type a lie for every other reader — and the
    # rounding mode has to be settled in SQL too, for the reason below.
    return _attendance_of(
        user,
        side,
        window,
        case(
            # **`Numeric`, not a Python float, and the difference is which way
            # a tie goes.** `100.0` binds as `float8`, `float8 / bigint`
            # resolves to `float8`, and `round(float8)` is `rint()` — half to
            # **even**. Three of eight sessions attended is `37.5`, published
            # as `38` here and as `37` before this line changed.
            #
            # Found by the review of the reviews percentage, which had the
            # identical shape and an explicit docstring promising the
            # opposite. One rounding, in one place, both times.
            (due > 0, cast(func.round(cast(came, Numeric) * 100 / due), Integer)),
            else_=None,
        ),
    )


def attendance_sessions(user: Any, side: Any) -> Any:
    """How many sessions ``attendance_rate`` is measured over: the "(12
    sessions)" beside "90%" on a pending card. **The rate's own denominator**,
    from the same base query, so the two cannot count different things. Zero,
    not null, when nothing is settled yet: that is "New mentee".

    Not `mentee_completed_sessions`, which counts sessions *received* and leaves
    out no-shows; "90% of 12" needs the no-shows in the 12."""
    return _attendance_of(user, side, None, _due())


def _due() -> Any:
    """Sessions where this party was expected and it is known whether they came."""
    return func.count().filter(SessionParticipant.attendance_status.in_(EXPECTED))


def _attendance_of(user: Any, side: Any, window: Window | None, column: Any) -> Any:
    """``column`` over this party's settled sessions on one side, starting inside
    ``window`` (all time for ``None``): the one base `attendance_rate` and
    `attendance_sessions` both read."""
    return (
        select(column)
        .select_from(_RATE)
        # Inner: a session with no participant row for this person carries no
        # attendance fact at all, and *unknown* must not enter a denominator.
        # Two of the 105 dev bookings have no tracker and so no participant rows.
        # **No `role` filter, and a mutation is why.** Removing it changed
        # nothing: `uq_session_participants_session_id` is unique on
        # `(session_id, user_id)`, so this join already matches at most one row
        # per session, and `ck_sessions_no_self_booking` stops that row belonging
        # to the other party. A condition that cannot exclude anything is not a
        # safeguard, it is a claim the reader has to verify — the fourth such
        # guard dropped rather than kept untestable.
        #
        # The join stays **inner** even though `FILTER` already ignores a null
        # attendance, so an outer join would return the same numbers. That is a
        # different thing from a dead condition: it is the shape of the query,
        # and the tighter shape drops rows before the aggregates rather than
        # after.
        .join(
            SessionParticipant,
            and_(
                SessionParticipant.session_id == _RATE.id,
                SessionParticipant.user_id == user,
            ),
        )
        .where(side == user, _RATE.status.in_(TERMINAL), during(_RATE.starts_at, window))
        .scalar_subquery()
    )


def canceller(actor_id: Any, actor_type: Any, *, other: str | None = BY_OTHER) -> Any:
    """Who a `cancelled` event's actor was to the session: `BY_MENTOR`,
    `BY_MENTEE`, or ``other`` for anybody else (decision 247). One rule for the
    dashboard's counts and the session read's `cancelled_by`, which passes
    ``other=None``."""
    as_user = actor_type == ActorType.USER
    return case(
        (and_(as_user, actor_id == Session.mentor_id), BY_MENTOR),
        (and_(as_user, actor_id == Session.mentee_id), BY_MENTEE),
        else_=other,
    )


def cancellations(mentor: Any, window: Bounds | None) -> Any:
    """The mentor's cancelled sessions whose cancellation happened inside
    ``window`` (ever, for ``None``), one row each, with when (`cancelled_at`)
    and by whom (`canceller`: `BY_MENTOR`, `BY_MENTEE` or `BY_OTHER`).

    **Who is the event's actor, acting as a user** (decision 247): `actor_id`
    names the mentor or the mentee and `actor_type` is `user`. An admin acting
    through the console is a different fact from the same person acting as
    themselves (`ActorType`), so it is `BY_OTHER`, as are the system and a
    cancellation that names nobody.

    **Placed by when the cancellation happened**, the `cancelled` event's
    `created_at`, not by `starts_at`: cancelling a session a month out is this
    week's act, and counting it only when the month arrives would hide it from
    the window it happened in. Held and missed sessions stay placed by
    `starts_at`, the time they were for.

    **The event says who, because no column does.** The transition writes
    `actor_id` on the `cancelled` event in the same transaction as the status
    (`session_writer.transitions`); the newest such event is the one read. A
    migrated cancellation names its canceller where legacy's `Canceled By`
    resolved to a user and nobody where it did not (orphan trackers); one with
    no event at all has no time and is in no window. The status is required
    too, so a row contradicting its own event is read by the column it is
    about (decision 101).

    A `LATERAL` joined after `status = 'cancelled'` has filtered the mentor's
    sessions, so the event lookup (`ix_session_events_session`) runs for
    cancelled sessions only.
    """
    event = (
        select(
            SessionEvent.actor_id,
            SessionEvent.actor_type,
            SessionEvent.created_at.label("cancelled_at"),
        )
        .where(
            SessionEvent.session_id == Session.id,
            SessionEvent.to_status == SessionStatus.CANCELLED,
        )
        .order_by(SessionEvent.created_at.desc(), SessionEvent.id.desc())
        .limit(1)
        .lateral("cancellation")
    )
    return (
        select(
            canceller(event.c.actor_id, event.c.actor_type).label("canceller"),
            event.c.cancelled_at,
        )
        .select_from(Session)
        .join(event, true())
        .where(
            Session.mentor_id == mentor,
            Session.status == SessionStatus.CANCELLED,
            during(event.c.cancelled_at, window),
        )
        .subquery("cancellations")
    )


def missed_by(party: Any) -> Any:
    """The predicate for a settled session ``party`` was expected at and missed.

    The rate's own terms, per session: `TERMINAL`, and the party's participant
    row in `MISSED`. So "missed 2" beside "attendance 80%" counts the sessions
    the rate's denominator holds and its numerator does not; a cancelled session
    is not missed, and `pending` or no row is unknown, not absent. Read from the
    participant row rather than `session_events.reason_code`, which names one
    party only when exactly one was absent and is null on migrated rows.

    `party` is a column of the enclosing `sessions` (`Session.mentor_id`).
    """
    return and_(Session.status.in_(TERMINAL), _recorded(party, MISSED))


def _recorded(party: Any, statuses: tuple[AttendanceStatus, ...]) -> Any:
    """``party``'s own participant row on the enclosing session is in ``statuses``."""
    return (
        select(SessionParticipant.id)
        .where(
            SessionParticipant.session_id == Session.id,
            SessionParticipant.user_id == party,
            SessionParticipant.attendance_status.in_(statuses),
        )
        .correlate(Session)
        .exists()
    )


#: Which side of a session a figure is about: the mentor's (delivered) or the
#: mentee's (received). A dual-role user's two sides never pool.
Side = Literal["mentor", "mentee"]


def _side(side: Side) -> Any:
    return Session.mentor_id if side == "mentor" else Session.mentee_id


def held(user: Any, window: Bounds | None, side: Side = "mentor") -> Any:
    """The predicate for a session ``user`` delivered (or, on the mentee side,
    received) that starts inside ``window``: the dashboards' *held*, and the
    rows the profile's four figures are over."""
    scope = delivered(user) if side == "mentor" else received(user)
    return and_(scope, during(Session.starts_at, window))


def held_count(mentor: Any, window: Bounds) -> Any:
    """How many sessions `held()` matches, as a scalar subquery."""
    return select(func.count()).select_from(Session).where(held(mentor, window)).scalar_subquery()


def missed(
    user: Any, window: Bounds | None, side: Side = "mentor", absent: Side | None = None
) -> Any:
    """The predicate for a settled session ``user`` is on as ``side``, starting
    inside ``window``, that the ``absent`` party missed (`missed_by()` on that
    party's participant row): ``user`` themselves by default, or the other
    party, as the mentor dashboard's `missed_by_mentee` reads it (decision 252).
    Each party is judged on their own row, so a session both missed matches
    whichever party is ``absent``."""
    party = _side(absent or side)
    return and_(_side(side) == user, during(Session.starts_at, window), missed_by(party))


def missed_count(mentor: Any, window: Bounds) -> Any:
    """How many of the mentor's sessions `missed()` matches, as a scalar subquery."""
    return select(func.count()).select_from(Session).where(missed(mentor, window)).scalar_subquery()


def _misses(mentor: Any, window: Bounds) -> Any:
    """The window's three miss counts in one pass over the mentor's sessions:
    their own (`missed_by_mentor`), their mentees' (`missed_by_mentee`, decision
    252), and their own while the mentee's row says they attended
    (`mentees_left_waiting`, decision 253). A session both missed, or a mentee
    whose attendance is `pending`, `left_early` or unrecorded, left nobody known
    to be waiting.

    Each figure is `missed()` itself as a `FILTER`; the `WHERE` repeats its
    scope, window and settled statuses only so the scan reads the mentor's
    settled sessions in the window, not every session."""
    mine = missed(mentor, window)
    return select(
        func.count().filter(mine).label("missed_by_mentor"),
        func.count().filter(missed(mentor, window, absent="mentee")).label("missed_by_mentee"),
        func.count()
        .filter(mine, _recorded(Session.mentee_id, ATTENDED))
        .label("mentees_left_waiting"),
    ).where(
        Session.mentor_id == mentor,
        during(Session.starts_at, window),
        Session.status.in_(TERMINAL),
    )


def current_streak(mentor: Any) -> Any:
    """How many sessions the mentor has held since their own last failure, as an
    uncorrelated scalar subquery (decision 250).

    The failures are this module's own rules, never restated: a session
    `missed()`, placed at its `starts_at`, and a cancellation `cancellations()`
    attributes `BY_MENTOR`, placed at its `cancelled_at` (decision 246 places it
    there too). The streak is the `held()` sessions starting strictly after the
    later of the two; all of them when there is neither (``GREATEST`` ignores a
    null, and ``-infinity`` stands in for no failure at all). One comparison, so
    each failure's lookup runs once.
    """
    cancelled = cancellations(mentor, None)
    last_cancelled = (
        select(func.max(cancelled.c.cancelled_at))
        .where(cancelled.c.canceller == BY_MENTOR)
        .scalar_subquery()
    )
    # Its own `sessions` scan, so it must not correlate to the count's.
    last_missed = (
        select(func.max(Session.starts_at))
        .where(missed(mentor, None))
        .correlate(None)
        .scalar_subquery()
    )
    never = cast(literal("-infinity"), TIMESTAMP(timezone=True))
    broken = func.greatest(last_cancelled, last_missed, never)
    return (
        select(func.count())
        .select_from(Session)
        .where(held(mentor, None), Session.starts_at > broken)
        .correlate(None)
        .scalar_subquery()
    )


def counted_minutes() -> Any:
    """The minutes one session counts: the longer of booked and measured
    (Integrations PRD, 2026-10-21, decision 4).

    ``GREATEST`` skips nulls in PostgreSQL, so an unmeasured session — Meet,
    migrated, records not read — counts its booked length, which is the whole
    reason ``measured_minutes`` is nullable rather than defaulted.
    """
    return func.greatest(Session.duration_minutes, Session.measured_minutes)


def _held_figures(counterpart: Any, among: Any = None) -> tuple[Any, Any, Any]:
    """Count, counted minutes and distinct ``counterpart`` of the sessions a
    statement reads, or of those matching ``among`` (a `FILTER`, so several
    windows share one scan): one definition for both sides, labelled by each
    caller."""

    def of(aggregate: Any) -> Any:
        return aggregate if among is None else aggregate.filter(among)

    return (
        of(func.count()),
        # `SUM` over no rows is `NULL`; zero is the real answer, and a nullable
        # count makes every client write the same fallback.
        func.coalesce(of(func.sum(counted_minutes())), 0),
        of(func.count(func.distinct(counterpart))),
    )


def _delivered_figures(mentor: Any, window: Window | None) -> Any:
    """The profile's four figures over sessions starting inside ``window``.

    One statement for the all-time profile and every dashboard window, so the
    two can only differ in which sessions they read.
    """
    sessions, minutes, mentees = _held_figures(Session.mentee_id)
    return select(
        sessions.label("completed_sessions"),
        minutes.label("mentoring_minutes"),
        mentees.label("mentees_mentored"),
        attendance_rate(mentor, MENTOR, window).label("attendance_rate"),
    ).where(held(mentor, window))


def mentee_window_figures(mentee: Any, window: Window | None) -> tuple[Any, Any, Any]:
    """`_delivered_figures` from the other side (decision 249): sessions the
    mentee received, their minutes and distinct mentors, starting inside
    ``window`` (all time for ``None``), as aggregates over a scan of
    ``Session`` rows; each is filtered, so one scan serves every window."""
    return _held_figures(Session.mentor_id, held(mentee, window, "mentee"))


def mentee_missed(mentee: Any, window: Window) -> Any:
    """How many sessions the mentee missed starting inside ``window``, as an
    aggregate over the same scan: `missed_by_mentor` from the other side."""
    return func.count().filter(missed(mentee, window, "mentee"))


def mentor_window_figures(mentor: Any, window: Window) -> Any:
    """Every session figure of one dashboard window, as a one-row select: the
    profile's four plus how many sessions the mentor missed, their mentees
    missed, and the mentor missed with the mentee waiting (by `starts_at`), and
    how many the mentor cancelled (by when they cancelled)."""
    figures = _delivered_figures(mentor, window).subquery("figures")
    misses = _misses(mentor, window).subquery("misses")
    cancelled = cancellations(mentor, window)
    by_mentor = (
        select(func.count()).select_from(cancelled).where(cancelled.c.canceller == BY_MENTOR)
    )
    return select(
        *figures.c,
        *misses.c,
        by_mentor.scalar_subquery().label("cancelled_by_mentor"),
    ).select_from(figures.join(misses, true()))


async def mentor_stats(session: AsyncSession, mentor: UUID) -> dict[str, Any]:
    """The four figures a profile shows, in one statement.

    Aggregates over no rows still produce a row, so a mentor who has delivered
    nothing comes back as zeros rather than as `None` — which is why this returns
    a dict rather than an optional and why the caller needs no fallback.

    **`mentoring_minutes` is `counted_minutes()`: the longer of booked and
    measured** (owner, Integrations PRD 2026-10-21, decision 4, reversing the
    "one definition" this docstring argued for). Daily's records give how long
    both parties were in the room, so an EduFurther video session that ran over
    counts the overrun; Google Meet cannot be measured — its conference records
    need domain-wide delegation on the *organiser's* Workspace, which a platform
    never holds for individual mentors — so it counts booked, as do migrated
    sessions. Booked is the floor, so no session counts less than it did.
    """
    row = (await session.execute(_delivered_figures(mentor, None))).mappings().one()
    return dict(row)
