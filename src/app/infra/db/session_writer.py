"""Creating a session. The first write to ``sessions`` this project has.

**Legality is asked of :func:`list_slots`, not re-derived here.** Whether an
instant may be booked depends on the offering's scheduling windows or the
mentor's general availability, the exceptions that subtract from either, the
notice window, the duration, and every session already on the calendar. All of
that is one function, and a second implementation of it here would be
non-negotiable #8 in its most expensive form — the copy that drifts silently
offers or refuses the wrong hour, and the two are tested apart so neither test
notices.

It also settles a question that would otherwise be answered twice: a slot the
public endpoint offers is a slot this endpoint accepts, by construction rather
than by agreement.

**The database is still the authority on conflicts.**
``sessions_no_mentor_double_booking`` is an ``EXCLUDE`` over the live statuses,
and the check above cannot replace it: between reading the slots and inserting
the row, another mentee can book the same hour. So the insert is attempted and
its refusal is mapped to a 409. Checking and then trusting the check is the race
the constraint exists to close.

**A refused booking leaves nothing behind.** The idempotency reservation is
written in this same transaction, so a rollback releases it and the client's
retry gets a clean attempt rather than being told forever that a request is in
flight.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import case, func, insert, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.domain.attendance import JOIN_CLOSES, join_window, within_join_window
from app.domain.enums import (
    ActorType,
    AttendanceStatus,
    SessionReasonCode,
    SessionRole,
    SessionStatus,
)
from app.domain.sessions import CANCELLATION_CUTOFF, TRANSITIONS, too_late_to_cancel
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.sessions import (
    Session,
    SessionEvent,
    SessionParticipant,
    SessionType,
    SessionTypeBookingConfig,
)
from app.infra.db.models.user import User
from app.infra.db.public_visibility import mentor_is_public, session_type_is_live
from app.infra.db.slot_store import list_slots

__all__ = [
    "DOUBLE_BOOKED",
    "book_session",
    "record_arrival",
    "settle_attendance",
    "transition",
]

#: The exclusion constraint's name, which is how a 409 is told from a 500.
#:
#: An insert here can violate three constraints — this one, ``no_self_booking``
#: and ``status_is_known`` — and only this one is the caller's ordinary bad luck.
#: Mapping every ``IntegrityError`` to 409 would report our own bug as the
#: client's conflict, and they would retry forever against a row that can never
#: be written.
DOUBLE_BOOKED = "sessions_no_mentor_double_booking"

#: How far either side of the requested instant to ask ``list_slots`` for.
#:
#: The slot grid is addressed in the **mentor's** calendar days while a booking
#: names a UTC instant, and the two disagree by up to fourteen hours. One day
#: either side covers every zone with room to spare, and the extra days cost a
#: handful of rows that the membership test discards.
SPAN_DAYS = 1


async def _whose(session: AsyncSession, session_type_id: UUID) -> UUID | None:
    """Which mentor an offering belongs to. **Unscoped, and nothing is returned
    from it.**

    Every predicate in ``public_visibility`` is written around a known mentor,
    because every other caller has one in the URL. Booking does not: the request
    names an offering and the mentor is *derived* from it, so the scope has to be
    found before it can be applied. Passing a column into
    ``session_type_is_live`` in place of the mentor id would make its ownership
    clause a tautology — the predicate would still read as scoped and would check
    nothing, which is the failure that file exists to prevent.

    So this reads the id and hands it straight back in as the scope, and its
    result reaches no response: an id that fails the real check below produces
    the same 404 as one that does not exist.
    """
    return (
        await session.execute(
            select(SessionType.mentor_user_id).where(SessionType.id == session_type_id)
        )
    ).scalar_one_or_none()


async def _offering(
    session: AsyncSession, session_type_id: UUID, mentor_id: UUID
) -> dict[str, Any] | None:
    """The offering as a stranger sees it, plus whether booking it needs an answer.

    **``COALESCE`` is the inherit rule, in the one place it is read.** A null on
    the config means *follow the mentor's own setting*; the mentor's column is
    ``NOT NULL``, so the chain always bottoms out — which is what makes the
    nullable boolean legitimate here and was not true of the primary-offering
    cascade it replaced.

    Visibility is the public predicate pair, spread unchanged: a booking is only
    possible where a slot is, and an offering a stranger cannot see is not one a
    stranger may book. Returning ``None`` for all six reasons is the same 404
    ``/slots`` already gives.
    """
    row = (
        (
            await session.execute(
                select(
                    SessionTypeBookingConfig.duration_minutes,
                    func.coalesce(
                        SessionTypeBookingConfig.requires_booking_confirmation,
                        MentorProfile.requires_booking_confirmation,
                    ).label("requires_confirmation"),
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
                    *session_type_is_live(mentor_id),
                    *mentor_is_public(),
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row else None


async def book_session(
    session: AsyncSession,
    mentee_id: UUID,
    payload: dict[str, Any],
    *,
    now: dt.datetime,
) -> UUID:
    """Book ``starts_at`` on an offering, and return the new session's id.

    Raises :class:`NotFoundError` when the offering is not publicly bookable —
    six reasons, one answer, the same conflation ``/slots`` makes.
    :class:`ValidationError` when the instant is not one the mentor offers, or
    when the mentee is the mentor. :class:`ConflictError` when the mentor was
    booked into that hour between the check and the write.

    **Does not commit.** The caller owns the transaction, because the
    idempotency reservation and this row are one unit — see the module
    docstring.
    """
    starts_at: dt.datetime = payload["starts_at"]
    mentor_id = await _whose(session, payload["session_type_id"])
    offering = (
        await _offering(session, payload["session_type_id"], mentor_id) if mentor_id else None
    )
    if offering is None or mentor_id is None:
        raise NotFoundError("no such bookable session type")

    if mentor_id == mentee_id:
        # `no_self_booking` would catch this, and a CHECK violation is a 500.
        # Refused here so the mentor who is also a mentee gets told why — dual
        # roles are free by design, so this is a reachable mistake rather than a
        # malformed request.
        raise ValidationError("you cannot book your own session type")

    # **The one legality check, and it is a membership test against the public
    # grid.** Asking for a span in the mentor's days and comparing instants
    # keeps the timezone question where it is already answered.
    day = starts_at.astimezone(dt.UTC).date()
    slots = await list_slots(
        session,
        mentor_id,
        payload["session_type_id"],
        start=day - dt.timedelta(days=SPAN_DAYS),
        end=day + dt.timedelta(days=SPAN_DAYS + 1),
        now=now,
    )
    if not slots or not any(slot.start == starts_at for slot in slots):
        # Deliberately not distinguished. "Too soon", "outside your hours",
        # "already taken" and "that is not on the grid" are all *this instant is
        # not offered*, and a client's only correct response to each is to
        # re-read `/slots` — which the message says.
        raise ValidationError("that time is not available — re-read the mentor's slots")

    status = (
        SessionStatus.PENDING_MENTOR_APPROVAL
        if offering["requires_confirmation"]
        else SessionStatus.CONFIRMED
    )

    try:
        session_id = (
            await session.execute(
                insert(Session)
                .values(
                    mentor_id=mentor_id,
                    mentee_id=mentee_id,
                    created_by=mentee_id,
                    session_type_id=payload["session_type_id"],
                    status=status,
                    starts_at=starts_at,
                    # **Snapshotted, never read live from the config.** Settled
                    # decision #10 gives the reason for a mentor's rate and it
                    # is the same one here: a later edit to the offering must
                    # not silently rewrite what was agreed.
                    duration_minutes=offering["duration_minutes"],
                    topic=payload.get("topic"),
                    booking_message=payload.get("booking_message"),
                )
                .returning(Session.id)
            )
        ).scalar_one()
    except IntegrityError as exc:
        if DOUBLE_BOOKED not in str(exc.orig):
            raise
        # Rolled back here rather than left to the caller: the transaction is
        # already aborted, so every later statement in it would fail with
        # `InFailedSQLTransaction` and bury this cause under that one.
        await session.rollback()
        raise ConflictError("that time was taken while you were booking it") from exc

    # **The participant rows, in the same transaction as the session**, which
    # is what `SessionParticipant`'s own docstring promises: written together,
    # they can never disagree with `mentor_id` and `mentee_id`, and the partial
    # unique index on `role = 'mentor'` is what catches it if they ever do.
    #
    # Both start `pending`, which is the honest state before the session runs
    # and is why the column is not nullable — "we do not know yet" is a real
    # answer and distinguishable from `no_show`. Nothing computes them for a
    # live session yet; the join-window sweep is the next release, and it needs
    # these rows to exist before it can.
    await session.execute(
        insert(SessionParticipant),
        [
            {"session_id": session_id, "user_id": mentor_id, "role": SessionRole.MENTOR},
            {"session_id": session_id, "user_id": mentee_id, "role": SessionRole.MENTEE},
        ],
    )

    # **The event is written by the same transaction as the status**, per the
    # model's own note: a trigger projecting one onto the other would be a
    # second mechanism for one fact.
    #
    # `from_status` is null because there is no prior state. That is the
    # creation event's signature and the reason the column is nullable.
    await session.execute(
        insert(SessionEvent).values(
            session_id=session_id,
            from_status=None,
            to_status=status,
            actor_id=mentee_id,
            actor_type=ActorType.USER,
        )
    )
    return session_id


async def transition(
    session: AsyncSession,
    session_id: UUID,
    actor_id: UUID,
    action: str,
    payload: dict[str, Any],
    *,
    now: dt.datetime,
) -> None:
    """Move one session along, and record who moved it and why.

    **Four endpoints, one function.** The rules live in
    :data:`app.domain.sessions.TRANSITIONS`, so accepting and declining differ
    by a table row rather than by a code path — which is the only shape in which
    "a mentee may never accept their own request" is enforced once instead of
    hoped for four times.

    Raises :class:`NotFoundError` when the session is not the caller's **or the
    action is not theirs to take**. Those are one answer deliberately, following
    ``require_admin``, which answers a non-admin with *no such endpoint* rather
    than a refusal: ``/sessions/{id}/accept`` is the mentor's decision resource,
    and to a mentee it does not exist. The mentee can still read the session, so
    nothing is being hidden that they could otherwise see.

    Raises :class:`ConflictError` when the caller is the right party and the
    session is in the wrong state, and when a cancellation lands inside the
    cutoff. Raises :class:`ValidationError` for a reason code this actor may not
    give.

    **Does not commit.** The caller owns the transaction, as everything in this
    module does.
    """
    rule = TRANSITIONS[action]

    row = (
        (
            await session.execute(
                select(Session.status, Session.starts_at, Session.mentor_id, Session.mentee_id)
                .where(Session.id == session_id)
                # **Scoped in the query on the write path**, not checked after
                # fetching. The roles this action permits are spread into the
                # `WHERE` rather than compared afterwards, so a mentee reaching
                # a mentor's action selects nothing at all.
                .where(
                    or_(
                        *(
                            Session.mentor_id == actor_id
                            if role is SessionRole.MENTOR
                            else Session.mentee_id == actor_id
                            for role in rule.by
                        )
                    )
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise NotFoundError("no such session")

    role = SessionRole.MENTOR if row["mentor_id"] == actor_id else SessionRole.MENTEE
    if SessionStatus(row["status"]) not in rule.allowed_from:
        raise ConflictError(f"a {row['status']} session cannot be {_past(action)}")
    if rule.honours_cutoff and too_late_to_cancel(row["starts_at"], now):
        minutes = int(CANCELLATION_CUTOFF.total_seconds() // 60)
        raise ConflictError(f"a session cannot be cancelled within {minutes} minutes of its start")

    reason_code = payload.get("reason_code")
    if reason_code is not None and reason_code not in rule.reasons.get(role, frozenset()):
        # A 422 rather than silently dropping it. The codes drive refund policy,
        # so a mentee sending the mentor's code is claiming a refund by choosing
        # a value — and a request that was partly honoured is worse than one
        # refused, because the client believes the reason was recorded.
        raise ValidationError(f"{reason_code} is not a reason you may give for {action}")

    await session.execute(update(Session).where(Session.id == session_id).values(status=rule.to))
    await session.execute(
        insert(SessionEvent).values(
            session_id=session_id,
            from_status=row["status"],
            to_status=rule.to,
            actor_id=actor_id,
            actor_type=ActorType.USER,
            reason_code=reason_code,
            reason_text=payload.get("reason_text"),
        )
    )


def _past(action: str) -> str:
    """`cancel` -> `cancelled`, for the refusal message.

    The stored status is the word a client already knows, so the message uses it
    rather than the verb — and it comes from the transition table rather than
    from string surgery, which would have to know that `cancel` doubles its `l`.
    """
    return str(TRANSITIONS[action].to)


async def record_arrival(
    session: AsyncSession, session_id: UUID, actor_id: UUID, *, now: dt.datetime
) -> None:
    """Mark the caller present at their own session.

    **Only their own row.** The `WHERE` names `user_id = actor_id`, so a mentor
    cannot mark a mentee present or a mentee vouch for a mentor — which matters
    because attendance drives both parties' reliability statistics, so marking
    somebody else present is editing their record.

    **Idempotent, and `joined_at` keeps the *first* arrival.** Pressing Join
    twice is the ordinary case — a dropped call, a refreshed tab — and the
    second press must not rewrite when they arrived. The `COALESCE` is what does
    that; without it the column would record the last press, which is the one
    fact nobody wants.

    Raises :class:`NotFoundError` when the session is not the caller's, and
    :class:`ConflictError` when it is not confirmed or the window is shut. Does
    not commit.
    """
    row = (
        (
            await session.execute(
                select(Session.status, Session.starts_at).where(
                    Session.id == session_id,
                    or_(Session.mentor_id == actor_id, Session.mentee_id == actor_id),
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise NotFoundError("no such session")
    if SessionStatus(row["status"]) is not SessionStatus.CONFIRMED:
        # A pending request has not been agreed to and a terminal one is over.
        # Both are `409` rather than a quiet no-op: a client that believes it has
        # registered an arrival will not try again.
        raise ConflictError(f"a {row['status']} session cannot be joined")
    if not within_join_window(row["starts_at"], now):
        opens, closes = join_window(row["starts_at"])
        raise ConflictError(
            f"this session can be joined between {opens.isoformat()} and {closes.isoformat()}"
        )

    await session.execute(
        update(SessionParticipant)
        .where(
            SessionParticipant.session_id == session_id,
            SessionParticipant.user_id == actor_id,
        )
        .values(
            joined_at=func.coalesce(SessionParticipant.joined_at, now),
            attendance_status=AttendanceStatus.ATTENDED,
        )
    )


def _window_shut(now: dt.datetime) -> Any:
    """Sessions whose join window has shut, in SQL.

    Built from :data:`JOIN_CLOSES` rather than from a literal, so this boundary
    and :func:`window_has_closed`'s are one definition rather than two that
    happen to agree today. A settlement running a minute early would mark
    somebody absent while they still had time to arrive.
    """
    shut = text(f"interval '{int(JOIN_CLOSES.total_seconds())} seconds'")
    return Session.starts_at + shut <= now


async def settle_attendance(session: AsyncSession, *, now: dt.datetime) -> int:
    """Decide every confirmed session whose join window has shut. Returns the count.

    **This is the producer `session_stats` has been waiting for.** That module
    records the problem plainly: `sessions.status` and
    `session_participants.attendance_status` are independent columns, a `CHECK`
    cannot tie them because it spans two tables, and *the migrated data already
    violates the invariant* — three sessions are `completed` with somebody
    absent. This settles sessions **booked after cutover** from their attendance,
    so from here forward there is one place the outcome is decided.

    It does not retrofit the migrated rows, and must not: those record what the
    legacy app believed, and rewriting them would destroy the evidence that the
    two figures disagree.

    **Set-based, in three statements plus the log.** The whole population is
    about a thousand sessions, so a per-session loop would buy nothing and cost
    a round trip each — and it would make a partial settlement reachable, where
    a session says `completed` while its participants still say `pending`.

    **Idempotent.** The participant update touches only `pending` rows and the
    session update only `confirmed` ones, so a second run settles nothing and
    writes no second event. That matters more than usual: this is driven by an
    external scheduler, because settled decision #13 rules out a platform-native
    cron — and an external scheduler is the kind that fires twice.

    Does not commit.
    """
    due = select(Session.id).where(Session.status == SessionStatus.CONFIRMED, _window_shut(now))

    # 1. Everybody still unknown was absent. `pending` only, so an arrival
    #    already recorded by `record_arrival` is left exactly as it is.
    await session.execute(
        update(SessionParticipant)
        .where(
            SessionParticipant.session_id.in_(due),
            SessionParticipant.attendance_status == AttendanceStatus.PENDING,
        )
        .values(attendance_status=AttendanceStatus.NO_SHOW)
    )

    # 2. The session's own outcome. Written as "no participant is absent" rather
    #    than "every participant attended" — the same thing today, and still
    #    right if `left_early` ever starts being written.
    #
    #    **This is `domain.attendance.outcome` expressed in SQL**, which is one
    #    rule in two places. The Python is the specification and this is the
    #    implementation, pinned by `test_the_settlement_agrees_with_the_rule`,
    #    which drives every combination through both and compares.
    absent = (
        select(SessionParticipant.id)
        .where(
            SessionParticipant.session_id == Session.id,
            SessionParticipant.attendance_status == AttendanceStatus.NO_SHOW,
        )
        .correlate(Session)
        .exists()
    )
    settled = (
        (
            await session.execute(
                update(Session)
                .where(Session.status == SessionStatus.CONFIRMED, _window_shut(now))
                .values(
                    status=case(
                        (absent, SessionStatus.NO_SHOW.value),
                        else_=SessionStatus.COMPLETED.value,
                    )
                )
                .returning(Session.id, Session.status)
            )
        )
        .mappings()
        .all()
    )
    if not settled:
        return 0

    # 3. The log. `actor_id` is null and `actor_type` is `system`, which the
    #    model's own docstring calls honest for a sweep and better than
    #    inventing a system user. `from_status` is `confirmed` on every row,
    #    because nothing else was selected.
    await session.execute(
        insert(SessionEvent),
        [
            {
                "session_id": row["id"],
                "from_status": SessionStatus.CONFIRMED,
                "to_status": row["status"],
                "actor_id": None,
                "actor_type": ActorType.SYSTEM,
                "reason_code": _absence_code(row["status"]),
            }
            for row in settled
        ],
    )
    return len(settled)


def _absence_code(status: str) -> SessionReasonCode | None:
    """The coded reason for a missed session, or none for one that happened.

    **One code for both absences, deliberately coarse.** The vocabulary has
    `MENTOR_NO_SHOW` and `MENTEE_NO_SHOW`, and choosing between them here would
    make the session-level event assert *which* party was absent — a fact the
    participant rows already hold precisely, and hold correctly when both were
    absent, which neither code can express. Duplicating it into the event is the
    second copy that drifts.

    So the event says *somebody was absent*, and the per-person truth stays in
    the one place that can state it.
    """
    if SessionStatus(status) is not SessionStatus.NO_SHOW:
        return None
    return SessionReasonCode.MENTEE_NO_SHOW
