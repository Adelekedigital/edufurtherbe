"""What a mentor has actually done, derived every time it is asked.

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

from typing import Any
from uuid import UUID

from sqlalchemy import Integer, Numeric, and_, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.domain.enums import AttendanceStatus, SessionStatus
from app.infra.db.models.sessions import Session, SessionParticipant

__all__ = [
    "ATTENDED",
    "EXPECTED",
    "MENTEE",
    "MENTOR",
    "TERMINAL",
    "attendance_rate",
    "delivered",
    "mentor_stats",
]

#: The mentor turned up.
ATTENDED = (AttendanceStatus.ATTENDED,)

#: The mentor was expected *and* it is known whether they came. Listing what
#: counts is how `pending` and `left_early` stay out without either being named.
EXPECTED = (AttendanceStatus.ATTENDED, AttendanceStatus.NO_SHOW)

#: Sessions that have happened, or should have. Not `cancelled` — nobody was
#: expected — and not `confirmed`, which has not happened yet.
TERMINAL = (SessionStatus.COMPLETED, SessionStatus.NO_SHOW)


def delivered(mentor: Any) -> Any:
    """The predicate for a session this mentor delivered.

    Scoped to `mentor_id`, because a mentor is also somebody's mentee and a
    session they *received* is not one they gave — the discovery card has a test
    for exactly that inversion.

    Served by `ix_sessions_mentor_completed`, a partial index that existed from
    the M4 schema with no reader until the card.
    """
    return and_(Session.mentor_id == mentor, Session.status == SessionStatus.COMPLETED)


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


def attendance_rate(user: Any, side: Any) -> Any:
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
    """
    came = func.count().filter(SessionParticipant.attendance_status.in_(ATTENDED))
    due = func.count().filter(SessionParticipant.attendance_status.in_(EXPECTED))
    return (
        # Cast in SQL, not in Python: `round()` returns numeric, so the value
        # arrives as "100.0" and `int()` refuses it. Fixing it at the boundary
        # would leave the column's type a lie for every other reader — and the
        # rounding mode has to be settled in SQL too, for the reason below.
        select(
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
            )
        )
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
        .where(side == user, _RATE.status.in_(TERMINAL))
        .scalar_subquery()
    )


async def mentor_stats(session: AsyncSession, mentor: UUID) -> dict[str, Any]:
    """The four figures a profile shows, in one statement.

    Aggregates over no rows still produce a row, so a mentor who has delivered
    nothing comes back as zeros rather than as `None` — which is why this returns
    a dict rather than an optional and why the caller needs no fallback.

    **`mentoring_minutes` is scheduled duration, not measured time**, and that is
    a limit rather than a shortcut. Daily's REST API exposes per-participant
    `join_time` and `duration`, so a measured figure is reachable there; Google
    Meet is not — its conference records need domain-wide delegation on the
    *organiser's* Workspace, which a platform never holds for individual mentors,
    and ADR 0012 requests no scope that would reach them. One number with two
    definitions depending on venue is worse than one honest definition.
    """
    statement = select(
        func.count().label("completed_sessions"),
        # `SUM` over no rows is `NULL`; zero is the real answer, and a nullable
        # count makes every client write the same fallback.
        func.coalesce(func.sum(Session.duration_minutes), 0).label("mentoring_minutes"),
        func.count(func.distinct(Session.mentee_id)).label("mentees_mentored"),
        attendance_rate(mentor, MENTOR).label("attendance_rate"),
    ).where(delivered(mentor))

    row = (await session.execute(statement)).mappings().one()
    return dict(row)
