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

import asyncio
import datetime as dt
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    BookingOverlapError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.domain.availability import BookingWindow
from app.domain.booking_limits import OVERLAP_MESSAGE
from app.domain.enums import (
    ActorType,
    QuestionType,
    SessionRole,
    SessionStatus,
)
from app.domain.intake import AskedQuestion, GivenAnswer, answer_problems
from app.domain.notifications import (
    Notification,
    recipients,
    reminders_for,
)
from app.domain.sessions import (
    respond_by,
)
from app.infra.clients.scheduler import SchedulerError
from app.infra.db.booking_rules import (
    effective_break_minutes,
    effective_duration_minutes,
    effective_requires_confirmation,
)
from app.infra.db.credit_writer import spend_credit
from app.infra.db.holds import active_hold, held_offer, holds_against, lock_mentor_slots
from app.infra.db.intake_file_store import link_files, usable_file_ids
from app.infra.db.intake_store import questions_by_type, record_answers
from app.infra.db.mentee_limits import check_mentee_limits
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.sessions import (
    Session,
    SessionEvent,
    SessionParticipant,
    SessionType,
    SessionTypeBookingConfig,
)
from app.infra.db.models.suggestions import SessionSuggestion
from app.infra.db.models.user import User
from app.infra.db.outbox import enqueue
from app.infra.db.public_visibility import mentor_is_public, session_type_is_live
from app.infra.db.session_writer.reminders import schedule_session_reminders
from app.infra.db.slot_store import offered_slot

logger = logging.getLogger(__name__)


#: The exclusion constraint's name, which is how a 409 is told from a 500.
#:
#: An insert here can violate three constraints — this one, ``no_self_booking``
#: and ``status_is_known`` — and only this one is the caller's ordinary bad luck.
#: Mapping every ``IntegrityError`` to 409 would report our own bug as the
#: client's conflict, and they would retry forever against a row that can never
#: be written.
DOUBLE_BOOKED = "sessions_no_mentor_double_booking"

#: The mentee's twin of ``DOUBLE_BOOKED`` (#342). Reached only by a race the
#: limits check lost, since that check refuses an overlap first.
MENTEE_DOUBLE_BOOKED = "sessions_no_mentee_double_booking"


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
                    # The length the slot was offered at (#216) — resolved the
                    # way `/slots` resolves it, so the two cannot disagree.
                    effective_duration_minutes().label("duration_minutes"),
                    # Measured against a held time under the lock (#339), the way
                    # the grid measures it.
                    effective_break_minutes().label("break_minutes"),
                    effective_requires_confirmation().label("requires_confirmation"),
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
    scheduler: Any = None,
    callback_url: str | None = None,
    external_busy: Any = None,
    require_answers: bool,
    window: BookingWindow,
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

    ``external_busy`` is handed straight to :func:`list_slots` and is why the
    mentor's own calendar is checked **inside the booking transaction** rather
    than only when the grid was rendered. Google free/busy is eventually
    consistent, so a slot list built seconds ago can miss a conflict; asking
    again here is the last look before the write. It costs one request on a path
    that already makes several, and it needs no second code path because
    legality was already asked of `list_slots` rather than re-derived.
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

    # **The answers, checked before anything is written** (#207), against this
    # offering's own live form — which is what keeps a question or option id
    # from another offering out of this booking.
    answers: list[dict[str, Any]] = payload.get("answers") or []
    form = (await questions_by_type(session, [payload["session_type_id"]])).get(
        payload["session_type_id"], []
    )
    file_ids = [a["file_id"] for a in answers if a.get("file_id") is not None]
    problems = answer_problems(
        [
            AskedQuestion(
                id=q["id"],
                question_type=QuestionType(str(q["question_type"])),
                is_required=bool(q["is_required"]),
                allows_multiple=bool(q["allows_multiple"]),
                option_ids=frozenset(o["id"] for o in q["options"]),
            )
            for q in form
        ],
        [
            GivenAnswer(
                question_id=a["question_id"],
                text=a.get("text"),
                option_ids=tuple(a["option_ids"]) if a.get("option_ids") is not None else None,
                file_id=a.get("file_id"),
            )
            for a in answers
        ],
        require_answers=require_answers,
        usable_files=await usable_file_ids(session, mentee_id, file_ids),
    )
    if problems:
        raise ValidationError(
            "; ".join(message for _, message in problems), field_errors=tuple(problems)
        )

    # **A time offered to this mentee** (#339), read before the grid so its notice
    # floor is measured from when it was offered: a slot valid then stays
    # bookable through its two-hour hold. Re-read under the lock below.
    pending_offer = await held_offer(
        session,
        mentor_id=mentor_id,
        mentee_id=mentee_id,
        session_type_id=payload["session_type_id"],
        starts_at=starts_at,
        now=now,
        lock=False,
    )

    # **The one legality check**, shared with suggesting a time (`offered_slot`).
    slot = await offered_slot(
        session,
        mentor_id,
        payload["session_type_id"],
        starts_at,
        now=now,
        window=window,
        external_busy=external_busy,
        # The time offered to *this* mentee at this offering is open to them;
        # every other hold is busy.
        holds_for=mentee_id,
        notice_from=pending_offer["created_at"] if pending_offer else None,
    )
    if slot is None:
        # Deliberately not distinguished. "Too soon", "outside your hours",
        # "already taken" and "that is not on the grid" are all *this instant is
        # not offered*, and a client's only correct response to each is to
        # re-read `/slots` — which the message says.
        raise ValidationError("that time is not available — re-read the mentor's slots")

    requires_confirmation = bool(offering["requires_confirmation"])
    status = (
        SessionStatus.PENDING_MENTOR_APPROVAL if requires_confirmation else SessionStatus.CONFIRMED
    )

    # **A hold is not a session, so the exclusion constraint cannot see it.**
    # The lock is taken *after* the grid was read, so two bookings racing for
    # one hour still meet at the constraint below as they always have; what it
    # adds is that a mentor suggesting this time either committed first — and
    # the hold is found here — or waits behind this booking and then finds the
    # hour taken (`holds.lock_mentor_slots`).
    await lock_mentor_slots(session, mentor_id)
    # **The time offered to this mentee, if this is it** (#339) — booked at the
    # length it was offered at, as decision #10 snapshots what was agreed, so a
    # mentor editing the offering inside the hold cannot change what it becomes.
    offer = await held_offer(
        session,
        mentor_id=mentor_id,
        mentee_id=mentee_id,
        session_type_id=payload["session_type_id"],
        starts_at=starts_at,
        now=now,
    )
    if pending_offer is not None and offer is None:
        # Spent or lapsed between the two reads; the grid was read on its terms.
        raise ConflictError("the suggested time is no longer held — re-read the mentor's slots")
    duration = int(offer["duration_minutes"] if offer else offering["duration_minutes"])
    if await holds_against(
        session,
        mentor_id,
        starts_at=starts_at,
        duration_minutes=duration,
        break_minutes=int(offering["break_minutes"]),
        now=now,
        except_id=offer["id"] if offer else None,
    ):
        raise ConflictError("that time is being held for another booking")

    # **The mentee's own limits (#342)**, under a per-mentee lock held to the
    # end of this transaction: no overlap, one live session per mentor, and the
    # overall cap. After legality, so an unoffered time is still a 422; and on
    # the length actually inserted — a suggested time's offered length (#339) —
    # so this checks the same window the database constraint will. Always taken
    # after the mentor's slot lock, the one order every path uses.
    await check_mentee_limits(
        session,
        mentee_id,
        mentor_id=mentor_id,
        starts_at=starts_at,
        duration_minutes=duration,
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
                    duration_minutes=duration,
                    topic=payload.get("topic"),
                    booking_message=payload.get("booking_message"),
                )
                .returning(Session.id)
            )
        ).scalar_one()
    except IntegrityError as exc:
        cause = str(exc.orig)
        if DOUBLE_BOOKED not in cause and MENTEE_DOUBLE_BOOKED not in cause:
            raise
        # Rolled back here rather than left to the caller: the transaction is
        # already aborted, so every later statement in it would fail with
        # `InFailedSQLTransaction` and bury this cause under that one.
        await session.rollback()
        if MENTEE_DOUBLE_BOOKED in cause:
            raise BookingOverlapError(OVERLAP_MESSAGE) from exc
        raise ConflictError("that time was taken while you were booking it") from exc

    # **The debit, in the same transaction as the session.** A session that
    # exists without its debit is a free booking; a debit without its session is
    # a credit taken for nothing. They commit together or not at all.
    #
    # After the insert rather than before it, so the ledger row can name the
    # session it paid for — which is the first of D8's four reasons the ledger
    # exists: *"I was charged for a session that never ran"* is only answerable
    # if the charge names the session.
    #
    # `spend_credit` takes an advisory lock on the mentee before reading their
    # balance, so two bookings arriving together serialise rather than both
    # passing a check-time-of-use test. A double-click cannot buy two sessions
    # with one credit.
    await spend_credit(session, mentee_id, session_id, now=now)

    # **A suggested time, taken up** (#339): the offer is spent by the booking
    # it became, in the same transaction, so it can never be booked twice.
    if offer is not None:
        spent = await session.scalar(
            update(SessionSuggestion)
            .where(
                # Owner-scoped and still active in the write itself, not only in
                # the read that found it.
                SessionSuggestion.id == offer["id"],
                SessionSuggestion.mentee_id == mentee_id,
                SessionSuggestion.mentor_id == mentor_id,
                *active_hold(now),
            )
            .values(accepted_session_id=session_id)
            .returning(SessionSuggestion.id)
        )
        if spent is None:
            # Unreachable while `held_offer` holds the row FOR UPDATE on the same
            # predicates; refused rather than trusted, so a booking can never
            # commit with its offer left open. Raising rolls the booking back.
            raise ConflictError("the suggested time is no longer held — re-read the mentor's slots")

    # **The answers, in the booking's transaction**: a session without the form
    # its mentee filled in, or a form for a session that was never written, are
    # both states nothing should be able to reach.
    # A file answer's upload is linked here, and the link is the guard: a file
    # another booking took since the check above refuses this one whole.
    keys = await link_files(
        session, session_id=session_id, uploader_id=mentee_id, file_ids=file_ids
    )
    answers = [
        a | {"file_storage_key": keys[a["file_id"]]} if a.get("file_id") is not None else a
        for a in answers
    ]
    await record_answers(
        session, session_id=session_id, mentee_id=mentee_id, answers=answers, form=form
    )

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

    # **The reminders, scheduled here and fired by a callback later.**
    #
    # Only for a request that waits: an auto-confirmed session has nobody to
    # nudge. And only for the ones still ahead — at the 24-hour booking floor
    # the deadline is eighteen hours away, so `t24` is already behind and
    # sending it now would be the booking message again, thirty seconds later.
    #
    # **A scheduling failure does not fail the booking.** The session exists and
    # holds its slot; the mentor is simply not nudged, and the deadline still
    # arrives on its own. Refusing the booking because a scheduler was slow
    # would lose something that cannot be recovered to protect something that
    # can.
    # **A session confirmed outright is real now**, so its own reminders are
    # published here. One that waits is not: telling both parties their session
    # is tomorrow, for a request nobody has accepted, is the bug this branch
    # exists to avoid. That one is scheduled at `/accept` instead.
    if not requires_confirmation:
        # Off the event loop (#370): four QStash round trips per booking.
        await asyncio.to_thread(
            schedule_session_reminders,
            session_id,
            starts_at,
            scheduler=scheduler,
            callback_url=callback_url or "",
            now=now,
        )

    deadline = respond_by(starts_at, requires_confirmation=requires_confirmation)
    if deadline is not None and scheduler is not None and callback_url:
        for kind, at in reminders_for(deadline, now=now):
            try:
                await asyncio.to_thread(
                    scheduler.schedule,
                    url=callback_url,
                    body={"session_id": str(session_id), "kind": kind},
                    at=at,
                )
            except (SchedulerError, NotImplementedError) as exc:
                logger.info("reminder %s for session %s not scheduled: %s", kind, session_id, exc)

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
