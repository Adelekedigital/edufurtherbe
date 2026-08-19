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
import logging
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, case, func, insert, or_, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.domain.attendance import (
    JOIN_CLOSES,
    AttendanceEvidence,
    join_window,
    within_join_window,
)
from app.domain.enums import (
    ActorType,
    AttendanceStatus,
    MeetingProvider,
    SessionReasonCode,
    SessionRole,
    SessionStatus,
)
from app.domain.meetings import plan_for
from app.domain.notifications import Notification, recipients
from app.domain.sessions import (
    CANCELLATION_CUTOFF,
    TRANSITIONS,
    respond_by,
    too_late_to_cancel,
)
from app.infra.clients.meetings import VenueUnavailableError, room_name
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.sessions import (
    Session,
    SessionEvent,
    SessionParticipant,
    SessionType,
    SessionTypeBookingConfig,
)
from app.infra.db.models.user import User
from app.infra.db.outbox import enqueue
from app.infra.db.public_visibility import mentor_is_public, session_type_is_live
from app.infra.db.session_type_store import resolve_venue
from app.infra.db.slot_store import list_slots

logger = logging.getLogger(__name__)

__all__ = [
    "DOUBLE_BOOKED",
    "book_session",
    "expire_requests",
    "provision_meeting",
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

#: Which message each transition sends. Beside the transition table rather than
#: inside it, because `TRANSITIONS` is a domain rule about *what may happen* and
#: this is a fact about what the platform then says — two vocabularies that move
#: for different reasons.
TRANSITION_NOTICE = {
    "accept": Notification.REQUEST_ACCEPTED,
    "decline": Notification.REQUEST_DECLINED,
    "withdraw": Notification.REQUEST_WITHDRAWN,
    "cancel": Notification.SESSION_CANCELLED,
}

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

    requires_confirmation = bool(offering["requires_confirmation"])
    status = (
        SessionStatus.PENDING_MENTOR_APPROVAL if requires_confirmation else SessionStatus.CONFIRMED
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
                    # Null unless the mentor has to answer. The two are decided
                    # by the same fact and are written together, so a confirmed
                    # session can never carry a deadline nobody is waiting on.
                    respond_by=respond_by(starts_at, requires_confirmation=requires_confirmation),
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

    # **Somebody is told, in this transaction.** Which of the two depends on
    # who already knows: an offering that confirms itself leaves the mentee
    # looking at a confirmation screen, so the mentor is the one learning
    # something — and a request that waits is the mentor's to answer. Both are
    # ADR 0025's rule rather than two decisions.
    booked = (
        Notification.SESSION_REQUESTED
        if status is SessionStatus.PENDING_MENTOR_APPROVAL
        else Notification.SESSION_BOOKED
    )
    await enqueue(
        session,
        booked,
        entity_type="session",
        entity_id=session_id,
        recipient_ids=recipients(booked, mentor_id=mentor_id, mentee_id=mentee_id),
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

    # **The party who did not act.** `cancel` is the only action either of them
    # may take, which is why `recipients` needs the actor at all — for the other
    # three the answer follows from the action.
    told = TRANSITION_NOTICE[action]
    await enqueue(
        session,
        told,
        entity_type="session",
        entity_id=session_id,
        recipient_ids=recipients(
            told,
            mentor_id=row["mentor_id"],
            mentee_id=row["mentee_id"],
            actor_id=actor_id,
        ),
        variables={"reason_text": payload.get("reason_text") or ""},
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

    recorded = await session.execute(
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
    # `rowcount` is a `CursorResult` attribute and `execute()` is typed as the
    # `Result` base, so the cast is a typing fact rather than a runtime one:
    # a DML statement always returns a cursor result.
    if not cast("CursorResult[Any]", recorded).rowcount:
        # **A silent success is the worst answer available here.** Every session
        # booked through this service has both rows, written in the same
        # transaction, so reaching this means a session that predates that or
        # one whose rows were removed — and the caller would otherwise be told
        # their arrival was recorded, walk away, and be settled as absent with
        # nothing to appeal against.
        raise ConflictError("this session has no attendance record for you")


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

    # 2. The session's own outcome: **both named parties recorded present**.
    #
    #    Counted rather than asked as "is anybody absent", which is the same
    #    question in the ordinary case and the wrong one at the edges — a
    #    session with one participant row, or with none, contains nobody absent
    #    and was settled as `completed`. `sessions` is 1:1 between exactly one
    #    mentor and one mentee by design (package D4), so the expected set is
    #    knowable and `= 2` is that invariant rather than a magic number. Group
    #    sessions would change this line, and would change the column layout
    #    that makes it possible.
    #
    #    **This is `domain.attendance.outcome` expressed in SQL**, which is one
    #    rule in two places. The Python is the specification and this is the
    #    implementation, pinned by `test_the_settlement_agrees_with_the_rule` —
    #    which used to drive only sessions created through booking, so it never
    #    reached the case the two disagreed on.
    def _came(party: Any) -> Any:
        return (
            select(SessionParticipant.id)
            .where(
                SessionParticipant.session_id == Session.id,
                SessionParticipant.user_id == party,
                SessionParticipant.attendance_status == AttendanceStatus.ATTENDED,
            )
            .correlate(Session)
            .exists()
        )

    mentor_came = _came(Session.mentor_id)
    mentee_came = _came(Session.mentee_id)
    settled = (
        (
            await session.execute(
                update(Session)
                .where(Session.status == SessionStatus.CONFIRMED, _window_shut(now))
                .values(
                    status=case(
                        (
                            and_(mentor_came, mentee_came),
                            SessionStatus.COMPLETED.value,
                        ),
                        else_=SessionStatus.NO_SHOW.value,
                    )
                )
                # The two facts travel with the row so the reason code can name
                # the party who was absent. PostgreSQL allows a subquery in
                # `RETURNING`, and it is evaluated against the *pre-update* row —
                # which is what is wanted: attendance is not what this statement
                # changes.
                .returning(
                    Session.id,
                    Session.status,
                    mentor_came.label("mentor_came"),
                    mentee_came.label("mentee_came"),
                )
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
    #
    #    **The evidence is recorded with the outcome**, and this is the first
    #    writer `session_events.metadata` has had. Today every outcome is
    #    `REPORTED` — both parties pressed a button and nothing observed the
    #    room. When a provider starts reporting join and leave, the same field
    #    carries `OBSERVED` and the two become distinguishable *retrospectively*,
    #    which is the whole reason to write it before anything reads it: a
    #    session settled last month cannot be re-examined for how it was judged.
    #
    #    It is what lets a later payout rule require observed attendance without
    #    a second status, and what stops `completed` quietly meaning two
    #    different things either side of the Daily integration.
    await session.execute(
        insert(SessionEvent),
        [
            {
                "session_id": row["id"],
                "from_status": SessionStatus.CONFIRMED,
                "to_status": row["status"],
                "actor_id": None,
                "actor_type": ActorType.SYSTEM,
                "reason_code": _absence_code(
                    mentor_came=bool(row["mentor_came"]), mentee_came=bool(row["mentee_came"])
                ),
                # `metadata_`, not `"metadata"`. The attribute is renamed to
                # dodge `Base.metadata`, and an ORM insert matches attribute
                # names — the column name is accepted in silence and the
                # server default written instead, which a test caught.
                "metadata_": {"evidence": AttendanceEvidence.REPORTED},
            }
            for row in settled
        ],
    )
    return len(settled)


def _absence_code(*, mentor_came: bool, mentee_came: bool) -> SessionReasonCode | None:
    """Which party was absent, when exactly one was.

    **Named when the vocabulary can say it, null when it cannot.** The first
    version returned `MENTEE_NO_SHOW` for every missed session, on the argument
    that the participant rows already hold the per-person truth and a session
    event asserting one party would be a second copy. Half of that argument
    survives and half does not: it is right that *both absent* has no correct
    code, and wrong that a mentor's absence should be filed under the mentee's —
    these codes are what refund policy runs on, so naming the wrong party is
    not coarseness, it is the wrong answer to the question that decides a
    refund.

    So: null when the session happened, null when both were absent — the
    participant rows are the only thing that can state that, and inventing a
    code would make an aggregate over `reason_code` wrong in a way nobody could
    see — and the exact code when exactly one party failed to turn up.
    """
    if mentor_came and mentee_came:
        return None
    if mentee_came:
        return SessionReasonCode.MENTOR_NO_SHOW
    if mentor_came:
        return SessionReasonCode.MENTEE_NO_SHOW
    return None


async def expire_requests(session: AsyncSession, *, now: dt.datetime) -> int:
    """Kill every unanswered request past its deadline. Returns the count.

    **This is what stops an abandoned request holding a mentor's hour forever.**
    `sessions_no_mentor_double_booking` covers `LIVE_STATUSES`, which includes
    `pending_mentor_approval`, and `slot_store._busy` counts it too — so until
    something writes a terminal status the slot is gone. `expired` is in
    `NEVER_AGREED`, so the moment this runs the hour comes back on both.

    **The status is `expired`, which the UI shows as "Unconfirmed".** The label
    differs from the stored value deliberately: nothing was declined and nobody
    withdrew, and calling it either would attribute a decision to a person who
    never made one.

    **Idempotent**, like the attendance sweep beside it and for the same reason:
    only `pending_mentor_approval` rows are touched, so a second run finds
    nothing and writes no second event. The two sweeps act on disjoint statuses,
    so their order in a run does not matter.

    Does not commit.
    """
    expired = (
        (
            await session.execute(
                update(Session)
                .where(
                    Session.status == SessionStatus.PENDING_MENTOR_APPROVAL,
                    Session.respond_by.is_not(None),
                    Session.respond_by <= now,
                )
                .values(status=SessionStatus.EXPIRED)
                .returning(Session.id, Session.mentor_id, Session.mentee_id)
            )
        )
        .mappings()
        .all()
    )
    if not expired:
        return 0

    # `actor_id` null with `actor_type` system: nobody decided this, and the
    # model's own docstring calls that honest for a sweep and better than
    # inventing a system user. The reason code is the one value in
    # `SessionReasonCode` that only a sweep can produce, which is why no party
    # is permitted to send it.
    await session.execute(
        insert(SessionEvent),
        [
            {
                "session_id": row["id"],
                "from_status": SessionStatus.PENDING_MENTOR_APPROVAL,
                "to_status": SessionStatus.EXPIRED,
                "actor_id": None,
                "actor_type": ActorType.SYSTEM,
                "reason_code": SessionReasonCode.EXPIRED_NO_RESPONSE,
            }
            for row in expired
        ],
    )

    # **Both parties, and this is the rule's one exception.** Nobody acted, so
    # neither of them already knows — the mentor let it lapse and the mentee has
    # been waiting on an answer that is no longer coming.
    for row in expired:
        await enqueue(
            session,
            Notification.REQUEST_EXPIRED,
            entity_type="session",
            entity_id=row["id"],
            recipient_ids=recipients(
                Notification.REQUEST_EXPIRED,
                mentor_id=row["mentor_id"],
                mentee_id=row["mentee_id"],
            ),
        )
    return len(expired)


async def provision_meeting(
    session: AsyncSession,
    session_id: UUID,
    *,
    rooms: Any,
    calendar: Any,
) -> None:
    """Give a newly confirmed session somewhere to meet. Does not commit.

    **Called at confirmation, which is two places rather than one:** booking, for
    an offering that auto-confirms, and ``/accept`` for one that does not. The
    model has always said the link is generated per session at confirmation —
    *"a static personal room means back-to-back sessions share it and an early
    joiner walks into the previous one"* — and until now nothing generated it.

    **The provider decides two independent things**, and `plan_for` holds the
    table because a reader who assumes *needs a room* and *wants a conference*
    are opposites gets Meet right and Daily wrong. Meet's link is a property of
    the calendar event; Daily's room is its own object created first; a custom
    venue is a URL that already exists.

    **A failure here does not fail the booking.** The session exists, the slot is
    held, and a link can be minted later — where refusing the booking because a
    third party was slow loses something that cannot be recovered. So the room
    and calendar calls are attempted and their absence is left as a null column
    rather than raised, which is also exactly what happens today with the null
    adapters wired.
    """
    row = (
        (
            await session.execute(
                select(
                    Session.status,
                    Session.session_type_id,
                    Session.starts_at,
                    Session.duration_minutes,
                    Session.meeting_url,
                ).where(Session.id == session_id)
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None or row["session_type_id"] is None:
        return
    # **The guard lives here, not at the two call sites.** Booking calls this
    # for every session and only some of them confirm, so a check at the caller
    # is a check somebody has to remember twice — and the cost of forgetting is
    # a room minted for a request that may be declined, which on a metered
    # provider is a room somebody pays for. Caught by a test asserting a pending
    # request gets nothing, which the first version of this failed.
    if SessionStatus(row["status"]) is not SessionStatus.CONFIRMED:
        return

    resolved = await resolve_venue(session, row["session_type_id"])
    if resolved is None:  # pragma: no cover - the offering was just booked
        return
    provider, custom_url = resolved
    plan = plan_for(provider)

    meeting_url: str | None = custom_url if plan.reuses_a_static_room else row["meeting_url"]
    external_room_id: str | None = None
    external_event_id: str | None = None

    # Only the opening edge: the room outlives the join window, so its close
    # comes from the session's own length rather than from the window.
    opens, _ = join_window(row["starts_at"])
    try:
        if plan.needs_room:
            room = rooms.create(
                name=room_name(str(session_id), provider),
                opens_at=opens,
                # The room outlives the join window by the session's own length:
                # a room that shut at `join_closes_at` would evict everybody
                # fifteen minutes into an hour-long session.
                closes_at=row["starts_at"] + dt.timedelta(minutes=row["duration_minutes"]),
            )
            meeting_url, external_room_id = room.url, room.external_id
    except (VenueUnavailableError, NotImplementedError) as exc:
        logger.info("no room for session %s: %s", session_id, exc)

    try:
        event = calendar.create_event(
            organiser_id=str(session_id),
            attendee_email="",
            starts_at=row["starts_at"],
            duration_minutes=int(row["duration_minutes"]),
            summary="EduFurther session",
            # **Only Meet, and this is the line that matters.** Asking for a
            # conference on a session held in Daily puts a second link on the
            # event, and the invitee clicks whichever the client renders first.
            wants_conference=plan.wants_conference,
            meeting_url=meeting_url,
        )
    except (VenueUnavailableError, NotImplementedError) as exc:
        logger.info("no calendar event for session %s: %s", session_id, exc)
        event = None

    if event is not None:
        external_event_id = event.external_id
        # Meet's link arrives by this path and no other, so it is taken only
        # when the event was asked for one — otherwise the URL we already had
        # stands.
        meeting_url = event.meeting_url or meeting_url

    await session.execute(
        update(Session)
        .where(Session.id == session_id)
        .values(
            meeting_provider=MeetingProvider(str(provider)),
            meeting_url=meeting_url,
            external_room_id=external_room_id,
            external_calendar_event_id=external_event_id,
        )
    )
