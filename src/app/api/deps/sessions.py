"""Sessions: reading, booking, transitions, joining, and their outbound clients."""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps.calendar import _free_busy
from app.api.deps.core import (
    CREATED,
    ENDPOINT_BOOKING,
    ENDPOINT_MOVE,
    CurrentUserDep,
    IdempotencyKeyHeader,
    SessionDep,
    TargetUserDep,
    _configured,
    claim_idempotency_key,
    logger,
)
from app.api.schemas.common import (
    MAX_PAGE_SIZE,
    clamp_limit,
    decode_session_cursor,
    encode_session_cursor,
)
from app.api.schemas.sessions import (
    MissedReasonWrite,
    RequestReminderWrite,
    SessionBookingWrite,
    SessionCancellationWrite,
    SessionDeclineWrite,
    SessionMoveWrite,
    SessionRead,
    SessionTransitionWrite,
)
from app.core.errors import (
    NotFoundError,
    ValidationError,
)
from app.domain.attendance import door_window, join_opens
from app.domain.availability import booking_window, local_day_start
from app.domain.enums import MeetingProvider, SessionRole, SessionStatus
from app.domain.refunds import RefundPolicy, refund_policy
from app.domain.sessions import TRANSITIONS, response_window
from app.infra.clients.meetings import (
    CalendarPort,
    RoomsPort,
    VenueUnavailableError,
    calendar_for,
    rooms_for,
)
from app.infra.clients.scheduler import SchedulerPort, scheduler_for
from app.infra.db.idempotency import Replayed, record_response
from app.infra.db.missed_reasons import send_missed_reason
from app.infra.db.onboarding_store import require_finished_onboarding
from app.infra.db.request_reminders import send_request_reminder

# `get_session` is aliased: this module already has one, and it is the **database
# session** dependency at line 142. Two callables with that name in one file is a
# collision a reader resolves by scrolling, and the wrong one is a plausible
# mistake rather than an obvious error — `bubble_id` shadowed a local the same
# way in the M4 transform and raised `UnboundLocalError` far from the edit.
from app.infra.db.session_store import (
    get_session as get_session_row,
)
from app.infra.db.session_store import (
    list_session_events,
    list_sessions,
)
from app.infra.db.session_writer import (
    FollowUps,
    add_meet_link,
    book_session,
    door_row,
    end_for_move,
    on_confirmed,
    on_ended,
    record_arrival,
    suggest_time,
    transition,
)

# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------
#
# **Two different scopes, and the difference is the URL.**
#
# The list is addressed by user — `/users/{id}/sessions` — so it takes
# `TargetUserDep`, and an admin reviewing somebody's sessions is the same
# implementation as that person reading their own.
#
# The single session and its events are addressed by session id, with no user in
# the path. There is no target user to resolve, so the scope is the **caller**:
# the query asks for a session this caller is a party to, and a session they are
# not party to is indistinguishable from one that does not exist. An admin is
# **not** admitted here — `/sessions/{id}` carries no user whose records an admin
# could be said to be reviewing, and widening it later is additive where
# narrowing would be breaking.


#: The dates a session filter accepts. Wide enough for any real calendar, and
#: away from the ends of `date`, where converting a local midnight to UTC
#: overflows into year 0 or 10000 and would surface as a 500, not a 422.
SESSION_FILTER_DATES = {"ge": dt.date(1900, 1, 1), "le": dt.date(9999, 12, 30)}


async def target_sessions(
    user_id: TargetUserDep,
    user: CurrentUserDep,
    session: SessionDep,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int | None, Query(ge=1, le=MAX_PAGE_SIZE)] = None,
    from_: Annotated[
        dt.date | None,
        Query(
            alias="from",
            description="First date, inclusive, in the caller's zone.",
            **SESSION_FILTER_DATES,
        ),
    ] = None,
    to: Annotated[
        dt.date | None,
        Query(description="Last date, exclusive, in the caller's zone.", **SESSION_FILTER_DATES),
    ] = None,
    status: Annotated[
        list[SessionStatus] | None,
        Query(description="Only these statuses; repeat the parameter for several."),
    ] = None,
    order: Annotated[
        Literal["asc", "desc"],
        Query(description="`desc` (default) is newest first; `asc` is soonest first."),
    ] = "desc",
    as_: Annotated[
        Literal["mentor", "mentee"] | None,
        Query(
            alias="as",
            description=(
                "Only the sessions where the user is on this side: `mentor` for "
                "those they mentor, `mentee` for those they booked. Absent is "
                "both, since one person may be either. Applied in the paged "
                "query, so `limit` counts only matching sessions."
            ),
        ),
    ] = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """One page of the sessions a user is a party to, optionally narrowed, and
    the cursor for the next — minted here because only here is the direction known.

    Dates are the **caller's** calendar dates — the person looking at a month —
    turned into instants at their local midnight, so a session late on the
    last evening of the month lands in that month for them.
    """
    if from_ is not None and to is not None and to <= from_:
        raise ValidationError(
            "to is exclusive and must be after from", field_errors=(("/to", "must be after from"),)
        )
    zone = str(user["timezone"])
    ascending = order == "asc"
    rows, has_more = await list_sessions(
        session,
        user_id,
        viewer_id=user["id"],
        limit=clamp_limit(limit),
        cursor=decode_session_cursor(cursor, ascending=ascending),
        starts_from=local_day_start(from_, zone) if from_ is not None else None,
        starts_before=local_day_start(to, zone) if to is not None else None,
        statuses=[s.value for s in status or ()],
        ascending=ascending,
        side=SessionRole(as_) if as_ is not None else None,
    )
    if not (has_more and rows):
        return rows, None
    last = rows[-1]
    return rows, encode_session_cursor(
        last["starts_at"].isoformat(), last["id"], ascending=ascending
    )


async def viewer_session(
    session_id: UUID, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    """One session, or a 404 that does not say which kind of 404 it is."""
    row = await get_session_row(session, session_id, user["id"])
    if row is None:
        raise NotFoundError("no such session")
    return row


async def viewer_session_events(
    session_id: UUID, user: CurrentUserDep, session: SessionDep
) -> list[dict[str, Any]]:
    """One session's history, scoped through the session itself.

    ``None`` from the store means the session is not the caller's, which is a
    404. An **empty list** means it is theirs and has no history — a different
    claim, and one that must not be used to answer the first case.
    """
    rows = await list_session_events(session, session_id, user["id"])
    if rows is None:
        raise NotFoundError("no such session")
    return rows


# --------------------------------------------------------------------------
# Booking
# --------------------------------------------------------------------------


#: What the fingerprint and the stored row call this endpoint. One string, so a
#: replay can never be served across endpoints because two literals drifted.
def _rooms(request: Request) -> RoomsPort:
    """The room provider, or the null one.

    **Read off `app.state` rather than constructed here**, following
    `app.state.storage`: the composition root wires a real adapter when one
    exists, and everything else keeps working against a default that creates
    nothing. `main.py` and this module are the sanctioned wiring points.
    """
    wired: RoomsPort | None = getattr(request.app.state, "meeting_rooms", None)
    if wired is not None:
        return wired
    # **Built per request rather than at startup**, so a key added to the
    # environment takes effect on the next deploy without a wiring change, and
    # so a test that sets no key gets the null adapter without unsetting
    # anything. The client it constructs is cheap; Daily is called at most twice
    # per session.
    return rooms_for(_configured(request))


def _join_lead(request: Request) -> dt.timedelta:
    """How long before the start the join window opens, from this app's settings.
    The one place the request layer reads it, so `/join`, `/door`, the room, the
    token and the published `join_opens_at` cannot each read it differently."""
    return join_opens(_configured(request))


#: The configured join lead, for a route that publishes `join_opens_at`.
JoinLeadDep = Annotated[dt.timedelta, Depends(_join_lead)]


def _refund_policy(request: Request) -> RefundPolicy:
    """The refund window this app runs on (`MENTEE_CANCEL_REFUND_HOURS`). The
    one place the request layer reads it, so the refund a cancellation pays and
    the `refund_until` every session publishes cannot disagree."""
    return refund_policy(_configured(request))


#: The configured refund policy, for a route that applies or publishes it.
RefundPolicyDep = Annotated[RefundPolicy, Depends(_refund_policy)]


def _follow_ups(request: Request) -> FollowUps:
    """What `on_confirmed` needs, wired from this app: the one place both
    confirmation points get it, so a new argument reaches both."""
    return FollowUps(
        rooms=_rooms(request),
        calendar=_calendar(request),
        scheduler=_scheduler(request),
        callback_url=_reminder_callback_url(request),
        app_base_url=_configured(request).app_base_url or "",
    )


def _calendar(request: Request) -> CalendarPort:
    wired: CalendarPort | None = getattr(request.app.state, "calendar", None)
    if wired is not None:
        return wired
    return calendar_for(_configured(request))


#: Where QStash is told to call back. One constant, because the value is
#: signed into the token QStash mints — so the path the scheduler publishes
#: and the path the verifier expects must be the same string, not two that
#: happen to agree.
REMINDER_CALLBACK_PATH = "/api/v1/callbacks/reminders"


async def booked_session(
    payload: SessionBookingWrite,
    user: CurrentUserDep,
    session: SessionDep,
    request: Request,
    idempotency_key: Annotated[str, IdempotencyKeyHeader],
) -> tuple[dict[str, Any], int, bool]:
    """Reserve the key, book the hour, store the answer — one transaction.

    **Required rather than optional, which is the one deviation from Stripe.**
    Stripe treats the header as recommended, and it can: its clients are servers
    written once. The retry here is a phone on a bad connection, and an optional
    header makes the guarantee opt-in for exactly the caller who most needs it.
    Requiring it now is also the safe direction to be wrong in — relaxing a
    required header later breaks nobody, and requiring an optional one breaks
    every client.

    **The key and the session commit together**, because a stored `201` for a
    session that was never written would replay the id of nothing. That also
    makes a refusal clean: `book_session` rolls back on a conflict, so the
    reservation goes with it and the client's next attempt starts fresh instead
    of being told forever that a request is in flight.

    **Read back through `get_session`, scoped to the caller.** A second query,
    deliberately: the response is the same shape `GET /sessions/{id}` returns,
    assembled by the same code, so the two cannot drift — and building it from
    the insert's own values would mean composing the party join by hand at the
    one moment there is no row to read it from.
    """
    # **Before the key is claimed**, so a refusal stores nothing and the same
    # key books once onboarding is finished. Booking only: reading, cancelling
    # and joining a session never pass here.
    await require_finished_onboarding(session, user["id"])
    reservation = await claim_idempotency_key(
        session,
        key=idempotency_key,
        user_id=user["id"],
        endpoint=ENDPOINT_BOOKING,
        body=payload.model_dump(mode="json"),
    )
    if isinstance(reservation, Replayed):
        return reservation.body, reservation.status_code, True

    # One instant for the booking and its reminders, so a nudge the booking
    # counted as ahead is not dropped as past after the venue calls.
    now = dt.datetime.now(dt.UTC)
    session_id = await book_session(
        session,
        user["id"],
        payload.model_dump(),
        now=now,
        scheduler=_scheduler(request),
        callback_url=_reminder_callback_url(request),
        external_busy=_free_busy(request),
        require_answers=_configured(request).require_intake_answers,
        window=booking_window(_configured(request)),
        response_window=response_window(_configured(request)),
    )
    # **In the booking's own transaction**, so a session cannot be committed
    # without whatever venue it was going to get. It no-ops unless the session
    # confirmed — a request that waits for the mentor gets its venue and
    # reminders at `/accept` — and that guard is inside `on_confirmed`, so
    # neither confirmation point has to remember it.
    await on_confirmed(session, session_id, _follow_ups(request), now=now)
    row = await get_session_row(session, session_id, user["id"])
    if row is None:  # pragma: no cover - the row was just written in this transaction
        raise NotFoundError("no such session")

    body = SessionRead.from_row(
        row, opens_before=_join_lead(request), refunds=_refund_policy(request)
    ).model_dump(mode="json")
    await record_response(session, reservation, status_code=CREATED, body=body)
    await session.commit()
    return body, CREATED, False


BookedSessionDep = Annotated[tuple[dict[str, Any], int, bool], Depends(booked_session)]


async def moved_session(
    session_id: UUID,
    payload: SessionMoveWrite,
    user: CurrentUserDep,
    session: SessionDep,
    request: Request,
    idempotency_key: Annotated[str, IdempotencyKeyHeader],
) -> tuple[dict[str, Any], int, bool]:
    """End the mentee's session and book its new time, in one transaction
    (decision 300); a refusal from either rolls both back.

    The new session is read back as booking reads it, and replayed by key as a
    booking is. The session moved is part of the fingerprint, so one key cannot
    replay a move of another session. The new venue is provisioned before the
    old calendar event is released, so a failure while provisioning has
    cancelled nobody's invite.
    """
    reservation = await claim_idempotency_key(
        session,
        key=idempotency_key,
        user_id=user["id"],
        endpoint=ENDPOINT_MOVE,
        body={"session_id": str(session_id), **payload.model_dump(mode="json")},
    )
    if isinstance(reservation, Replayed):
        return reservation.body, reservation.status_code, True

    now = dt.datetime.now(dt.UTC)
    old = await end_for_move(
        session,
        session_id,
        user["id"],
        now=now,
        refunds=_refund_policy(request),
        reason_text=payload.message,
    )
    # Booking's own gate, after the session was found to be the caller's, so
    # anyone else still gets the 404. A refusal rolls the whole move back.
    await require_finished_onboarding(session, user["id"])
    new_id = await book_session(
        session,
        user["id"],
        {
            "session_type_id": old["session_type_id"],
            "starts_at": payload.starts_at,
            "topic": old["topic"],
            # The mentee's note about the move, which the mentor reads on the
            # session and in its booking email; else the old note carries.
            "booking_message": payload.message or old["booking_message"],
        },
        now=now,
        scheduler=_scheduler(request),
        callback_url=_reminder_callback_url(request),
        external_busy=_free_busy(request),
        require_answers=False,
        window=booking_window(_configured(request)),
        response_window=response_window(_configured(request)),
        moved_from={"id": session_id, "mentor_id": old["mentor_id"]},
    )
    await on_confirmed(session, new_id, _follow_ups(request), now=now)
    await on_ended(session, session_id, calendar=_calendar(request))
    row = await get_session_row(session, new_id, user["id"])
    if row is None:  # pragma: no cover - the row was just written in this transaction
        raise NotFoundError("no such session")
    body = SessionRead.from_row(
        row, opens_before=_join_lead(request), refunds=_refund_policy(request)
    ).model_dump(mode="json")
    await record_response(session, reservation, status_code=CREATED, body=body)
    await session.commit()
    return body, CREATED, False


MovedSessionDep = Annotated[tuple[dict[str, Any], int, bool], Depends(moved_session)]


def _scheduler(request: Request) -> SchedulerPort:
    """The real scheduler when a token is configured, and a loud nothing else.

    Built per call rather than wired at startup, following `_rooms`: a token
    added to the environment takes effect on the next deploy without a wiring
    change, and a test that sets none gets the null adapter without unsetting
    anything.

    **Read off `app.state` first**, which `_rooms`, `_calendar` and `_free_busy`
    all do and this did not. The inconsistency was invisible until something
    needed to assert *that a reminder was published* rather than what happened
    when one was: there was no seam to put a fake in.
    """
    wired: SchedulerPort | None = getattr(request.app.state, "scheduler", None)
    if wired is not None:
        return wired
    return scheduler_for(_configured(request))


def _reminder_callback_url(request: Request) -> str | None:
    """Where QStash should call back, or ``None`` if we cannot say.

    **Stated in configuration rather than derived from the request.** A service
    behind a proxy cannot see the URL the caller used, and the signature names
    its destination — so a derived value that is wrong rejects every callback
    with a message about signatures rather than about configuration, which is
    the hardest kind of misconfiguration to diagnose.
    """
    return callback_url(request, REMINDER_CALLBACK_PATH)


def callback_url(request: Request, path: str) -> str | None:
    """``path`` on this service's configured public origin, or ``None`` when
    there is none. The one builder of a QStash destination: the scheduler
    publishes to it and the verifier expects it as the token's `sub`."""
    base = _configured(request).public_base_url
    return f"{base.rstrip('/')}{path}" if base else None


def transitions(action: str) -> Callable[..., Awaitable[None]]:
    """Build the dependency for one named transition.

    **A factory rather than four copies**, and the argument is safe in a way the
    `include_inactive` flag `session_type_is_live` refused was not: `action` is
    a literal fixed at each of the four call sites below, never a value a caller
    can send. A mis-defaulted flag there would have made a deactivated offering
    bookable; there is no default here to get wrong.

    Every rule the action carries — who may take it, from which state, which
    reason codes they may give — is looked up in `domain/sessions.py` by this
    name, so the four differ by a table row rather than by a code path.
    """

    async def run(
        session_id: UUID,
        user: dict[str, Any],
        session: AsyncSession,
        request: Request,
        payload: SessionTransitionWrite | None,
    ) -> None:
        now = dt.datetime.now(dt.UTC)
        body = payload.model_dump() if payload else {}
        suggested = body.pop("suggested_starts_at", None)
        await transition(
            session,
            session_id,
            user["id"],
            action,
            body,
            now=now,
            refunds=_refund_policy(request),
            # A suggestion needs no reason code, and tells the mentee itself, in
            # one email with the news.
            offers_another_time=suggested is not None,
        )
        # **Another time offered, in the same transaction** (#339): the session
        # ends as it would have, and the offer is written beside it — or, if the
        # offer is refused, neither lands.
        if suggested is not None:
            await suggest_time(
                session,
                session_id,
                user["id"],
                suggested,
                ended_as=str(TRANSITIONS[action].to),
                reason_text=body.get("reason_text"),
                now=now,
                window=booking_window(_configured(request)),
                external_busy=_free_busy(request),
                scheduler=_scheduler(request),
                callback_url=_reminder_callback_url(request),
            )
        # The second confirmation point. Accepting is the moment a
        # confirmation-required session becomes real, and it is the only action
        # that produces `confirmed` — declining, withdrawing and cancelling all
        # end a session rather than starting one.
        if action == "accept":
            # The second place a session becomes real: booking covers the
            # offering that confirms itself, this the one that waited.
            await on_confirmed(session, session_id, _follow_ups(request), now=now)
        else:
            # **Every other transition ends the session.** Decline, withdraw
            # and cancel are the three; `accept` is the only one that creates
            # rather than releases.
            await on_ended(session, session_id, calendar=_calendar(request))
        await session.commit()

    # **Two signatures over one body**, because the annotation is the contract.
    # FastAPI reads it to build the request schema, so cancelling can only ask
    # its extra question by being annotated differently — and `accept` must not
    # inherit a field it would have to ignore. The body stays in one place; only
    # the shape the client sends differs. **The three endings require a body**,
    # because each requires a `reason_code` or an offer (decision 237); `accept`
    # takes none at all.
    async def cancelling(
        session_id: UUID,
        user: CurrentUserDep,
        session: SessionDep,
        request: Request,
        payload: SessionCancellationWrite,
    ) -> None:
        await run(session_id, user, session, request, payload)

    async def declining(
        session_id: UUID,
        user: CurrentUserDep,
        session: SessionDep,
        request: Request,
        payload: SessionDeclineWrite,
    ) -> None:
        await run(session_id, user, session, request, payload)

    async def withdrawing(
        session_id: UUID,
        user: CurrentUserDep,
        session: SessionDep,
        request: Request,
        payload: SessionTransitionWrite,
    ) -> None:
        await run(session_id, user, session, request, payload)

    async def accepting(
        session_id: UUID, user: CurrentUserDep, session: SessionDep, request: Request
    ) -> None:
        await run(session_id, user, session, request, None)

    if action == "cancel":
        return cancelling
    if action == "decline":
        return declining
    if action == "withdraw":
        return withdrawing
    return accepting


AcceptedSessionDep = Annotated[None, Depends(transitions("accept"))]
DeclinedSessionDep = Annotated[None, Depends(transitions("decline"))]
WithdrawnSessionDep = Annotated[None, Depends(transitions("withdraw"))]
CancelledSessionDep = Annotated[None, Depends(transitions("cancel"))]


async def _door_for(
    request: Request,
    session: AsyncSession,
    session_id: UUID,
    row: dict[str, Any],
    user: dict[str, Any],
) -> str | None:
    """Turn a venue row into the caller's way in, or ``None`` if there is none.

    **Shared by the arrival and the door** (#379), which is why it is a function
    rather than the tail of `joined_session` it used to be: both have to mint the
    same credential with the same owner rule and the same expiry, and two copies
    of a token request are where one drifts — the mentor losing owner rights on
    one path, or an expiry tied to the join window on the other.

    **The URL is minted here rather than stored**, and only for a private room.
    A Daily room refuses anybody without a token, and storing the token instead
    would put two live bearer credentials per session into the database and
    every backup, outliving the session they open. For every other venue the
    door is the stored URL: a Meet link is on the calendar event, and a custom
    venue is the address the mentor typed.

    **A Meet session may not have its link yet** (#384): it is added at the last
    reminder, five minutes out, and the join window opens before that. So the
    first way in adds it, committed so the other party is handed the same Meet.
    """
    stored = row["meeting_url"]
    if not stored:
        # The writer decides whether this venue gets a Meet; any other is
        # simply left with no way in, as before.
        link = await add_meet_link(session, session_id, calendar=_calendar(request))
        await session.commit()
        return link
    if row["meeting_provider"] != MeetingProvider.DAILY or not row["external_room_id"]:
        return str(stored) if stored else None

    # **The token's validity is the door's window, read from the one place it is
    # defined.** It was rebuilt here from its parts, which a review caught as the
    # same rule kept twice: change `door_window` and `/door` would accept a
    # request while this minted a token Daily had already stopped honouring.
    opens, closes = door_window(
        row["starts_at"], int(row["duration_minutes"]), opens_before=_join_lead(request)
    )
    try:
        # **Off the event loop.** `token_for` is a blocking HTTP call with a
        # fifteen-second timeout, and this route is async — so run inline, a
        # slow Daily stalls every request on the worker, not just this one. That
        # matters more here than it did on `/join` alone: a door is asked for on
        # every reconnect, and a flaky connection reconnects often.
        #
        # A thread, as for every outbound call on an async path (#370).
        token = await asyncio.to_thread(
            _rooms(request).token_for,
            room=str(row["external_room_id"]),
            user_id=str(user["id"]),
            user_name=str(user.get("first_name") or "Guest"),
            # The mentor hosts: on Daily an owner may admit, mute and end.
            is_owner=bool(row["is_mentor"]),
            opens_at=opens,
            closes_at=closes,
        )
    except (VenueUnavailableError, NotImplementedError) as exc:
        # **Not a failure of the request.** What is missing is a way in, and
        # saying so honestly beats a 500 on a request that otherwise did what it
        # was asked.
        logger.info("no door for session %s: %s", session_id, exc)
        return None

    return f"{stored}?t={token}"


async def joined_session(
    session_id: UUID, user: CurrentUserDep, session: SessionDep, request: Request
) -> str | None:
    """Record that the caller arrived, and hand back the door.

    **Not a transition, and not in the table above.** Arriving changes no
    status: a session stays `confirmed` while it runs, and what it becomes is
    decided once for both parties when the join window shuts. Putting this in
    `TRANSITIONS` would have needed a `to` state it does not have.

    The door it hands back is `_door_for`'s, shared with `/door` — where the
    reasons a Daily URL is minted rather than stored are written down once.
    """
    now = dt.datetime.now(dt.UTC)
    row = await record_arrival(
        session, session_id, user["id"], now=now, opens_before=_join_lead(request)
    )
    # Committed before the door is minted: the arrival is the thing asked for,
    # and a provider failure afterwards must not take it back.
    await session.commit()
    return await _door_for(request, session, session_id, row, user)


async def session_door(
    session_id: UUID, user: CurrentUserDep, session: SessionDep, request: Request
) -> str | None:
    """The caller's way back into a running session, **recording nothing** (#379).

    For the party who dropped at minute twenty, or refreshed the tab. `/join`
    stops at `join_closes_at` because that is when the outcome becomes
    decidable; this runs to the session's end because getting in has no such
    constraint, and the token it mints already lasts that long.

    Calling it never touches `joined_at` or `attendance_status`, so a party who
    only ever used this would be settled absent — which is right: pressing Join
    is the signal of arrival, and this is a door, not an arrival.
    """
    row = await door_row(
        session,
        session_id,
        user["id"],
        now=dt.datetime.now(dt.UTC),
        opens_before=_join_lead(request),
    )
    # **Released before Daily is called**, as `joined_session` commits before
    # it. The read left a transaction open on a pooled connection, and the
    # token request is a blocking call with a fifteen-second timeout — so a slow
    # provider would otherwise hold one connection idle-in-transaction per
    # refreshing participant, and enough of them starve unrelated endpoints.
    # Rolled back rather than committed because there is nothing to keep.
    await session.rollback()
    return await _door_for(request, session, session_id, row, user)


JoinedSessionDep = Annotated[str | None, Depends(joined_session)]
SessionDoorDep = Annotated[str | None, Depends(session_door)]


async def sent_missed_reason(
    session_id: UUID, payload: MissedReasonWrite, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    """The caller's reason for missing ``session_id``, written and committed.

    The caller is the only sender: the store reads the session scoped to them
    and writes their own id, never one from the body.
    """
    row = await send_missed_reason(
        session, session_id, user["id"], reason=payload.reason, note=payload.note
    )
    await session.commit()
    return row


SentMissedReasonDep = Annotated[dict[str, Any], Depends(sent_missed_reason)]


async def reminded_session(
    session_id: UUID, payload: RequestReminderWrite, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    """The mentee's reminder sent and committed, then the session as they now read it.

    The caller is the only sender: the store names them as the mentee in the
    write itself, so a refusal has written nothing (decision 294).
    """
    await send_request_reminder(
        session, session_id, user["id"], message=payload.message, now=dt.datetime.now(dt.UTC)
    )
    await session.commit()
    row = await get_session_row(session, session_id, user["id"])
    if row is None:  # pragma: no cover - the caller is its mentee, just written
        raise NotFoundError("no such session")
    return row


RemindedSessionDep = Annotated[dict[str, Any], Depends(reminded_session)]
SessionsPageDep = Annotated[tuple[list[dict[str, Any]], str | None], Depends(target_sessions)]
SessionDetailDep = Annotated[dict[str, Any], Depends(viewer_session)]
SessionEventsDep = Annotated[list[dict[str, Any]], Depends(viewer_session_events)]
