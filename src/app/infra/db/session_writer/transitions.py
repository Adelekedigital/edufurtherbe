"""Moving a session between states: accept, decline, withdraw, cancel, and
the system's own expiry of requests nobody answered in time.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.domain.enums import (
    ActorType,
    SessionReasonCode,
    SessionRole,
    SessionStatus,
)
from app.domain.notifications import (
    Notification,
    recipients,
)
from app.domain.refunds import RefundPolicy, never_agreed_refund, transition_refund
from app.domain.sessions import (
    CANCELLATION_CUTOFF,
    TRANSITIONS,
    records_unavailability,
    too_late_to_cancel,
)
from app.infra.db.availability_writer import block_session_window
from app.infra.db.credit_writer import refund_credit
from app.infra.db.models.sessions import (
    Session,
    SessionEvent,
)
from app.infra.db.models.user import User
from app.infra.db.outbox import enqueue
from app.infra.db.pending_requests import lapsed_request
from app.infra.db.session_writer.meetings import release_meeting

#: What a mentor reads beside this block in their own availability.
#:
#: Free text rather than a link, because the exception carries no foreign key to
#: the session and should not: once written it is the mentor's row to keep or
#: delete, and a block that vanished when a session row changed would be a
#: surprise. The cost is that nothing can later count which blocks came from
#: cancellations.
BLOCKED_BY_CANCELLATION = "Kept from a cancelled session"

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


async def transition(
    session: AsyncSession,
    session_id: UUID,
    actor_id: UUID,
    action: str,
    payload: dict[str, Any],
    *,
    now: dt.datetime,
    refunds: RefundPolicy,
    notify: bool = True,
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

    ``notify=False`` leaves the message to the caller: a decline or cancel that
    carries a suggested time (#339) tells the mentee both in one email, sent by
    the suggestion, rather than two seconds apart.
    """
    rule = TRANSITIONS[action]

    row = (
        (
            await session.execute(
                select(
                    Session.status,
                    Session.starts_at,
                    Session.mentor_id,
                    Session.mentee_id,
                    Session.duration_minutes,
                    # **The mentor's own zone, not the rule's.** This block
                    # appears on their calendar beside the ones they wrote by
                    # hand, so it is stored the way those are.
                    User.timezone,
                    # The deadline, from the one clause the sweep and `/me`
                    # also read — so a request the badge no longer counts is
                    # one the mentor can no longer answer.
                    and_(*lapsed_request(now)).label("lapsed"),
                )
                .join(User, User.id == Session.mentor_id)
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
    if role is SessionRole.MENTOR and row["lapsed"]:
        # Past `respond_by` the request is expired in all but the stored word,
        # which the hourly sweep writes; answering it now would confirm an hour
        # the mentee was told had lapsed.
        raise ConflictError(
            f"this request passed its answer deadline and cannot be {_past(action)}"
        )
    if rule.honours_cutoff and too_late_to_cancel(row["starts_at"], now):
        minutes = int(CANCELLATION_CUTOFF.total_seconds() // 60)
        raise ConflictError(f"a session cannot be cancelled within {minutes} minutes of its start")

    reason_code = payload.get("reason_code")
    if reason_code is not None and reason_code not in rule.reasons.get(role, frozenset()):
        # A 422 rather than silently dropping it. Each side reports with its own
        # codes, so a mentee sending the mentor's would misfile the reason — and
        # a request that was partly honoured is worse than one refused, because
        # the client believes the reason was recorded.
        raise ValidationError(f"{reason_code} is not a reason you may give for {action}")

    await session.execute(update(Session).where(Session.id == session_id).values(status=rule.to))

    # **The hour goes back on the grid by default**, because `cancelled` is in
    # `FREES_THE_HOUR`. A mentor who says they are not free records that as an
    # availability exception instead — the mechanism that already means it,
    # which they can see and remove. Session state is not where unavailability
    # lives; that reading is what kept a cancelled hour hidden and unbookable
    # with nothing able to release it.
    if records_unavailability(action, role, release_slot=bool(payload.get("release_slot", True))):
        await block_session_window(
            session,
            row["mentor_id"],
            starts_at=row["starts_at"],
            duration_minutes=row["duration_minutes"],
            timezone=row["timezone"],
            reason=BLOCKED_BY_CANCELLATION,
        )
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

    # **Whether the mentee's credit comes back is `domain.refunds`'s call**
    # (decision 229): a request that never became a session always refunds; a
    # mentor's cancellation always refunds; a mentee's only with the
    # deployment's notice (`refunds`, twelve hours unless configured). Asked here
    # and written here, so the status and the refund commit together — a
    # session marked cancelled with the credit still owed is the state a retry
    # cannot fix, because the transition already happened.
    owed = transition_refund(
        rule.to, actor=role, starts_at=row["starts_at"], now=now, policy=refunds
    )
    if owed is not None:
        await refund_credit(session, row["mentee_id"], session_id, reason=owed, now=now)

    # **The party who did not act.** `cancel` is the only action either of them
    # may take, which is why `recipients` needs the actor at all — for the other
    # three the answer follows from the action.
    if not notify:
        return
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
        # What the event says, kept on the row: the code is worded and the side
        # is named only at send time (`domain/messages.py`), so a party who has
        # since deleted their account is not named (#288).
        variables={
            "reason_text": payload.get("reason_text") or "",
            "reason_code": str(reason_code or ""),
            "cancel_initiator": str(role),
        },
    )


def _past(action: str) -> str:
    """`cancel` -> `cancelled`, for the refusal message.

    The stored status is the word a client already knows, so the message uses it
    rather than the verb — and it comes from the transition table rather than
    from string surgery, which would have to know that `cancel` doubles its `l`.
    """
    return str(TRANSITIONS[action].to)


async def expire_requests(session: AsyncSession, *, now: dt.datetime, calendar: Any = None) -> int:
    """Kill every unanswered request past its deadline. Returns the count.

    **This is what stops an abandoned request holding a mentor's hour forever.**
    `sessions_no_mentor_double_booking` covers `LIVE_STATUSES`, which includes
    `pending_mentor_approval`, and `slot_store._busy` counts it too — so until
    something writes a terminal status the slot is gone. `expired` is in
    `FREES_THE_HOUR`, so the moment this runs the hour comes back on both.

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
                .where(*lapsed_request(now))
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

    # **The same refund, for the request nobody answered.** `refund_credit` is
    # once-per-session at the database, so this sweep re-running — which it does
    # every hour, and which its own docstring calls idempotent — pays each
    # request back exactly once. The reason comes from the same rule the
    # transitions ask, so "an expired request refunds" is written once.
    owed = never_agreed_refund(SessionStatus.EXPIRED)
    for row in expired:
        if owed is not None:
            await refund_credit(session, row["mentee_id"], row["id"], reason=owed, now=now)

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

    # **An expired request ends a session too, so its event goes with it.**
    # The calendar is optional here where it is required elsewhere: this runs in
    # a sweep that predates the integration, and a caller passing none simply
    # leaves the ids in place for a later run to clear.
    #
    # `expired` is a sequence of row mappings, not of ids — it is built with
    # `.mappings()` and the notification block above reads `row["id"]` from it.
    # This loop was written against a branch where it was ids, and taking it
    # verbatim would hand `release_meeting` a mapping where it wants a `UUID`.
    if calendar is not None:
        for row in expired:
            await release_meeting(session, row["id"], calendar=calendar)
    return len(expired)
