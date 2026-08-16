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

**So nothing enforces the invariant and the data already violates it.**
`sessions.status` and `session_participants.attendance_status` are independent
columns, and a `CHECK` cannot tie them because it spans two tables.

Every stat here therefore answers from the column it is *about* — the session
says delivered, so it counts; the participant says absent, so the rate falls —
and a reader seeing "11 sessions, 69%" is looking at a contradiction that is
really in the data rather than at a number hiding it. The session lifecycle must
derive status *from* attendance so there is one place it is decided; until then
this is honest rather than correct, which is the better of the two available.

ATTENDANCE COUNTS TWO STATUSES AND IGNORES TWO
==============================================
The rate is the mentor's own: sessions they attended over sessions they were
expected at.

    numerator     attendance_status = 'attended'
    denominator   attendance_status IN ('attended', 'no_show')

`pending` is *unknown*, not absent — its own docstring says "we do not know yet
is a real answer and distinguishable from `NO_SHOW`" — and until the lifecycle
that marks attendance exists, it is the state of every session booked after
cutover. Counting it as absence would report every new mentor as unreliable.

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

from sqlalchemy import Integer, and_, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import AttendanceStatus, SessionStatus
from app.infra.db.models.sessions import Session, SessionParticipant

__all__ = ["ATTENDED", "EXPECTED", "TERMINAL", "delivered", "mentor_stats"]

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


def _attendance_rate(mentor: UUID) -> Any:
    """The mentor's own attendance, as a whole-number percentage or ``NULL``.

    A scalar subquery rather than a join, so the ratio is computed where both
    counts are in scope and the outer query stays one row.

    **`NULL`, never zero, when nothing is known.** Zero percent says "never shows
    up"; null says "no data yet". A mentor whose sessions have not been marked up
    must not be branded the first — and since the lifecycle that marks them does
    not exist, that is currently every mentor with a session booked after
    cutover.
    """
    came = func.count().filter(SessionParticipant.attendance_status.in_(ATTENDED))
    due = func.count().filter(SessionParticipant.attendance_status.in_(EXPECTED))
    return (
        # Cast in SQL, not in Python: `round()` returns numeric, so the value
        # arrives as "100.0" and `int()` refuses it. Fixing it at the boundary
        # would leave the column's type a lie for every other reader.
        select(
            case(
                (due > 0, cast(func.round(100.0 * came / due), Integer)),
                else_=None,
            )
        )
        .select_from(Session)
        # Inner: a session with no mentor participant row carries no attendance
        # fact at all, and *unknown* must not enter a denominator. Two of the 105
        # dev bookings have no tracker and so no participant rows.
        # **No `role` filter, and a mutation is why.** Removing it changed
        # nothing: `uq_session_participants_session_id` is unique on
        # `(session_id, user_id)`, so this join already matches at most one row
        # per session, and `ck_sessions_no_self_booking` stops that row belonging
        # to anyone but the mentor. A condition that cannot exclude anything is
        # not a safeguard, it is a claim the reader has to verify — the fourth
        # such guard dropped rather than kept untestable.
        #
        # The join stays **inner** even though `FILTER` already ignores a null
        # attendance, so an outer join would return the same numbers. That is a
        # different thing from a dead condition: it is the shape of the query,
        # and the tighter shape drops rows before the aggregates rather than
        # after.
        .join(
            SessionParticipant,
            and_(
                SessionParticipant.session_id == Session.id,
                SessionParticipant.user_id == mentor,
            ),
        )
        .where(Session.mentor_id == mentor, Session.status.in_(TERMINAL))
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
        _attendance_rate(mentor).label("attendance_rate"),
    ).where(delivered(mentor))

    row = (await session.execute(statement)).mappings().one()
    return dict(row)
