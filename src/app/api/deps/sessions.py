"""Sessions: reading, booking, transitions, joining, and their outbound clients."""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps.calendar import _free_busy
from app.api.deps.core import (
    CREATED,
    ENDPOINT_BOOKING,
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
    decode_cursor,
)
from app.api.schemas.sessions import (
    SessionBookingWrite,
    SessionCancellationWrite,
    SessionRead,
    SessionTransitionWrite,
)
from app.core.errors import (
    NotFoundError,
    ValidationError,
)
from app.domain.attendance import join_window
from app.domain.availability import booking_window, local_day_start
from app.domain.enums import MeetingProvider, SessionStatus
from app.infra.clients.meetings import (
    DailyRooms,
    GoogleCalendar,
    NullCalendar,
    NullRooms,
    VenueUnavailableError,
)
from app.infra.clients.scheduler import (
    NullScheduler,
    QStashScheduler,
)
from app.infra.db.idempotency import Replayed, record_response

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
    book_session,
    provision_meeting,
    record_arrival,
    release_meeting,
    schedule_session_reminders,
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
) -> tuple[list[dict[str, Any]], bool]:
    """One page of the sessions a user is a party to, optionally narrowed.

    Dates are the **caller's** calendar dates — the person looking at a month —
    turned into instants at their local midnight, so a session late on the
    last evening of the month lands in that month for them.
    """
    if from_ is not None and to is not None and to <= from_:
        raise ValidationError(
            "to is exclusive and must be after from", field_errors=(("/to", "must be after from"),)
        )
    zone = str(user["timezone"])
    return await list_sessions(
        session,
        user_id,
        limit=clamp_limit(limit),
        cursor=decode_cursor(cursor),
        starts_from=local_day_start(from_, zone) if from_ is not None else None,
        starts_before=local_day_start(to, zone) if to is not None else None,
        statuses=[s.value for s in status or ()],
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
def _rooms(request: Request) -> Any:
    """The room provider, or the null one.

    **Read off `app.state` rather than constructed here**, following
    `app.state.storage`: the composition root wires a real adapter when one
    exists, and everything else keeps working against a default that creates
    nothing. `main.py` and this module are the sanctioned wiring points.
    """
    wired = getattr(request.app.state, "meeting_rooms", None)
    if wired is not None:
        return wired
    # **Built per request rather than at startup**, so a key added to the
    # environment takes effect on the next deploy without a wiring change, and
    # so a test that sets no key gets the null adapter without unsetting
    # anything. The client it constructs is cheap; Daily is called at most twice
    # per session.
    key = _configured(request).daily_api_key
    return DailyRooms(key.get_secret_value()) if key else NullRooms()


def _calendar(request: Request) -> Any:
    wired = getattr(request.app.state, "calendar", None)
    if wired is not None:
        return wired
    settings = _configured(request)
    # **All three or none.** A refresh token is useless without the client that
    # minted it, and two of three configured is the shape most likely to be a
    # half-finished setup — failing to `NullCalendar` there is quieter than
    # failing every booking with an OAuth error.
    if not (
        settings.google_oauth_client_id
        and settings.google_oauth_client_secret
        and settings.google_calendar_refresh_token
    ):
        return NullCalendar()
    return GoogleCalendar(
        client_id=settings.google_oauth_client_id,
        client_secret=settings.google_oauth_client_secret.get_secret_value(),
        refresh_token=settings.google_calendar_refresh_token.get_secret_value(),
        calendar_id=settings.google_calendar_id,
    )


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
    reservation = await claim_idempotency_key(
        session,
        key=idempotency_key,
        user_id=user["id"],
        endpoint=ENDPOINT_BOOKING,
        body=payload.model_dump(mode="json"),
    )
    if isinstance(reservation, Replayed):
        return reservation.body, reservation.status_code, True

    session_id = await book_session(
        session,
        user["id"],
        payload.model_dump(),
        now=dt.datetime.now(dt.UTC),
        scheduler=_scheduler(request),
        callback_url=_reminder_callback_url(request),
        external_busy=_free_busy(request),
        require_answers=_configured(request).require_intake_answers,
        window=booking_window(_configured(request)),
    )
    # **In the booking's own transaction**, so a session cannot be committed
    # without whatever venue it was going to get. It no-ops unless the session
    # confirmed — a request that waits for the mentor gets its link at
    # `/accept` — and that guard is inside `provision_meeting` rather than here,
    # because this call site and the transition one would both have to remember
    # it.
    await provision_meeting(session, session_id, rooms=_rooms(request), calendar=_calendar(request))
    row = await get_session_row(session, session_id, user["id"])
    if row is None:  # pragma: no cover - the row was just written in this transaction
        raise NotFoundError("no such session")

    body = SessionRead.from_row(row).model_dump(mode="json")
    await record_response(session, reservation, status_code=CREATED, body=body)
    await session.commit()
    return body, CREATED, False


BookedSessionDep = Annotated[tuple[dict[str, Any], int, bool], Depends(booked_session)]


def _scheduler(request: Request) -> Any:
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
    wired = getattr(request.app.state, "scheduler", None)
    if wired is not None:
        return wired
    settings = _configured(request)
    token = settings.qstash_token
    return (
        QStashScheduler(token.get_secret_value(), settings.qstash_url) if token else NullScheduler()
    )


def _reminder_callback_url(request: Request) -> str | None:
    """Where QStash should call back, or ``None`` if we cannot say.

    **Stated in configuration rather than derived from the request.** A service
    behind a proxy cannot see the URL the caller used, and the signature names
    its destination — so a derived value that is wrong rejects every callback
    with a message about signatures rather than about configuration, which is
    the hardest kind of misconfiguration to diagnose.
    """
    base = _configured(request).public_base_url
    return f"{base.rstrip('/')}{REMINDER_CALLBACK_PATH}" if base else None


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
        await transition(
            session,
            session_id,
            user["id"],
            action,
            payload.model_dump() if payload else {},
            now=dt.datetime.now(dt.UTC),
        )
        # The second confirmation point. Accepting is the moment a
        # confirmation-required session becomes real, and it is the only action
        # that produces `confirmed` — declining, withdrawing and cancelling all
        # end a session rather than starting one.
        if action == "accept":
            await provision_meeting(
                session, session_id, rooms=_rooms(request), calendar=_calendar(request)
            )
            # **The second place a session becomes real**, and therefore the
            # second place its reminders are published. Booking covers the
            # offering that confirms itself; this covers the one that waited.
            # Missing it would leave every confirmation-required session
            # silently unreminded — which is exactly the shape of gap that made
            # `release_meeting` necessary.
            row = await get_session_row(session, session_id, user["id"])
            if row is not None:
                schedule_session_reminders(
                    session_id,
                    row["starts_at"],
                    scheduler=_scheduler(request),
                    callback_url=_reminder_callback_url(request) or "",
                    now=dt.datetime.now(dt.UTC),
                )
        else:
            # **Every other transition ends the session**, and an ended session
            # must not leave a live event in either calendar. Decline, withdraw
            # and cancel are the three; `accept` is the only one that creates
            # rather than releases.
            await release_meeting(session, session_id, calendar=_calendar(request))
        await session.commit()

    # **Two signatures over one body**, because the annotation is the contract.
    # FastAPI reads it to build the request schema, so cancelling can only ask
    # its extra question by being annotated differently — and `accept` must not
    # inherit a field it would have to ignore. The body stays in one place; only
    # the shape the client sends differs.
    async def cancelling(
        session_id: UUID,
        user: CurrentUserDep,
        session: SessionDep,
        request: Request,
        payload: SessionCancellationWrite | None = None,
    ) -> None:
        await run(session_id, user, session, request, payload)

    async def ending(
        session_id: UUID,
        user: CurrentUserDep,
        session: SessionDep,
        request: Request,
        payload: SessionTransitionWrite | None = None,
    ) -> None:
        await run(session_id, user, session, request, payload)

    return cancelling if action == "cancel" else ending


AcceptedSessionDep = Annotated[None, Depends(transitions("accept"))]
DeclinedSessionDep = Annotated[None, Depends(transitions("decline"))]
WithdrawnSessionDep = Annotated[None, Depends(transitions("withdraw"))]
CancelledSessionDep = Annotated[None, Depends(transitions("cancel"))]


async def joined_session(
    session_id: UUID, user: CurrentUserDep, session: SessionDep, request: Request
) -> str | None:
    """Record that the caller arrived, and hand back the door.

    **Not a transition, and not in the table above.** Arriving changes no
    status: a session stays `confirmed` while it runs, and what it becomes is
    decided once for both parties when the join window shuts. Putting this in
    `TRANSITIONS` would have needed a `to` state it does not have.

    **The URL is minted here rather than stored**, and only for a private room.
    A Daily room refuses anybody without a token, so recording an arrival and
    returning nothing would close none of the gap this endpoint exists for —
    and storing the token instead would put two live bearer credentials per
    session into the database and every backup, outliving the session they open.

    For every other venue the door is the stored URL: a Meet link is on the
    calendar event, and a custom venue is the address the mentor typed.
    """
    now = dt.datetime.now(dt.UTC)
    row = await record_arrival(session, session_id, user["id"], now=now)
    await session.commit()

    stored = row["meeting_url"]
    if row["meeting_provider"] != MeetingProvider.DAILY or not row["external_room_id"]:
        return str(stored) if stored else None

    # Only the opening edge: the token outlives the join window for the same
    # reason the room does — one expiring at `join_closes_at` would evict its
    # holder fifteen minutes into an hour-long session.
    opens, _ = join_window(row["starts_at"])
    try:
        token = _rooms(request).token_for(
            room=str(row["external_room_id"]),
            user_id=str(user["id"]),
            user_name=str(user.get("first_name") or "Guest"),
            # The mentor hosts: on Daily an owner may admit, mute and end.
            is_owner=bool(row["is_mentor"]),
            opens_at=opens,
            # The room outlives the window by the session's length, and so must
            # the token — one expiring at `join_closes_at` would evict its
            # holder fifteen minutes into an hour.
            closes_at=row["starts_at"] + dt.timedelta(minutes=int(row["duration_minutes"])),
        )
    except (VenueUnavailableError, NotImplementedError) as exc:
        # **Not a failure of the join.** The arrival is recorded and committed;
        # what is missing is a way in, and saying so honestly beats a 500 on a
        # request that already did the thing it was asked to do.
        logger.info("no door for session %s: %s", session_id, exc)
        return None

    return f"{stored}?t={token}"


JoinedSessionDep = Annotated[str | None, Depends(joined_session)]

SessionsPageDep = Annotated[tuple[list[dict[str, Any]], bool], Depends(target_sessions)]
SessionDetailDep = Annotated[dict[str, Any], Depends(viewer_session)]
SessionEventsDep = Annotated[list[dict[str, Any]], Depends(viewer_session_events)]
