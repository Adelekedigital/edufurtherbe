"""Sessions a user is a party to, what happened to each one, and the five writes.

Booking, then the four transitions — accept, decline, withdraw, cancel. What is
still absent is the *refund* policy, which is a different thing from the
cancellation rule: the transitions capture `reason_code`, which is the input
policy will read, and nothing prices it yet.

**The four transitions are four names for one table.** Who may take each action,
from which state, and which reason codes they may give all live in
`domain/sessions.py`, so "a mentee may never accept their own request" is
enforced once rather than hoped for four times.

**Two addressing schemes, because they answer different questions.**
``/users/{id}/sessions`` is "this person's sessions", so it takes
``TargetUserDep`` and an admin reviewing somebody's history runs the same code
as that person reading their own. ``/sessions/{id}`` is "this session", and
there is no user in the path for an admin to be reviewing — so it is scoped to
the **caller**, and a session they are not part of is indistinguishable from one
that does not exist.

The list returns every session the user is a party to, mentor or mentee, in one
page. A user may be both — dual roles are free by design, because authorization
is profile existence rather than a role column — and each row carries both ids
so a client can tell which side they were on.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.api.deps import (
    AcceptedSessionDep,
    BookedSessionDep,
    CancelledSessionDep,
    DeclinedSessionDep,
    JoinedSessionDep,
    SessionDetailDep,
    SessionEventsDep,
    SessionsPageDep,
    WithdrawnSessionDep,
)
from app.api.schemas.common import Page, encode_cursor
from app.api.schemas.sessions import SessionEventRead, SessionRead

router = APIRouter(prefix="/api/v1", tags=["sessions"])

READ_RESPONSES: dict[int | str, dict[str, str]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "The bearer token is absent, malformed, expired or wrongly signed."
    },
    status.HTTP_404_NOT_FOUND: {
        "description": (
            "No such session, **or** you are not part of it. Indistinguishable "
            "on purpose — distinguishing them tells anyone who can enumerate "
            "ids which sessions exist."
        )
    },
}

LIST_RESPONSES: dict[int | str, dict[str, str]] = {
    **READ_RESPONSES,
    status.HTTP_404_NOT_FOUND: {
        "description": (
            "No such user, **or** not yours. Indistinguishable on purpose. A "
            "platform admin may read another user's sessions."
        )
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "description": "The `cursor` was not one this endpoint issued."
    },
}


@router.get(
    "/users/{user_id}/sessions",
    response_model=Page[SessionRead],
    summary="List a user's sessions",
    description=(
        "Every session this user is a party to — as mentor, as mentee, or "
        "both — newest first.\n\n"
        "A user with no sessions gets an empty page and a `200`, not a `404`: "
        "an empty collection exists, it simply has nothing in it, and a `404` "
        "would leave a client unable to tell that from being refused.\n\n"
        "**Cancelled and missed sessions are included.** They are still that "
        "person's history, and omitting them would silently shorten it.\n\n"
        "Each row carries `mentor_id` and `mentee_id`, so a client can tell "
        "which side of the session this user was on without a second call."
    ),
    responses=LIST_RESPONSES,
)
async def list_user_sessions(page: SessionsPageDep) -> Page[SessionRead]:
    rows, has_more = page
    next_cursor = (
        encode_cursor(rows[-1]["starts_at"].isoformat(), rows[-1]["id"])
        if has_more and rows
        else None
    )
    return Page(data=[SessionRead.from_row(row) for row in rows], next_cursor=next_cursor)


@router.get(
    "/sessions/{session_id}",
    response_model=SessionRead,
    summary="Read one session",
    description=(
        "One session, readable by the mentor and the mentee in it.\n\n"
        "**Not by an admin.** This URL names no user whose records an admin "
        "could be said to be reviewing — use `/users/{id}/sessions` for that. "
        "Widening this later is additive; narrowing it after a client has built "
        "against it is not."
    ),
    responses=READ_RESPONSES,
)
async def read_session(session: SessionDetailDep) -> SessionRead:
    return SessionRead.from_row(session)


@router.get(
    "/sessions/{session_id}/events",
    response_model=Page[SessionEventRead],
    summary="Read a session's history",
    description=(
        "Every state this session passed through, oldest first — who acted, "
        "when, and why.\n\n"
        "`next_cursor` is always `null`: a session's history is bounded and is "
        "returned whole. The envelope is here anyway because ADR 0016 puts it "
        "on every list, and a bare JSON array can never gain a sibling field "
        "without breaking every client already reading it.\n\n"
        "Append-only and immutable: a row states what happened at a moment, and "
        "a fact that can be edited is not a log.\n\n"
        "A **null `actor_id`** means no person acted — an expiry or no-show "
        "sweep — or that a migrated row records an action the legacy app did "
        "not attribute. Read it alongside `actor_type`.\n\n"
        "`reason_code` is null on every migrated event: the legacy app held "
        "only free text, so there is nothing to code them from."
    ),
    responses=READ_RESPONSES,
)
async def read_session_events(events: SessionEventsDep) -> Page[SessionEventRead]:
    return Page(data=[SessionEventRead.from_row(row) for row in events], next_cursor=None)


BOOKING_RESPONSES: dict[int | str, dict[str, str]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "The bearer token is absent, malformed, expired or wrongly signed."
    },
    status.HTTP_404_NOT_FOUND: {
        "description": (
            "No such bookable offering. **Six reasons, one answer** — the "
            "offering does not exist, is switched off, is deleted, or its "
            "mentor is unapproved, unlisted or gone. The same conflation "
            "`/slots` makes, for the same reason: telling them apart tells "
            "anyone who can guess an id which mentors exist and what state "
            "they are in."
        )
    },
    status.HTTP_409_CONFLICT: {
        "description": (
            "The mentor was booked into that hour while you were booking it, "
            "**or** a request carrying this `Idempotency-Key` is still in "
            "flight. Both are states that pass on their own — re-read `/slots` "
            "and try again."
        )
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "description": (
            "The body failed validation, the instant is not one this mentor "
            "offers, this `Idempotency-Key` already stands for a different "
            "request, or you are the mentor."
        )
    },
}

#: Stripe's header, and the name is borrowed rather than invented so a client
#: library that already understands one idempotent API understands this one.
REPLAYED_HEADER = "Idempotent-Replayed"


@router.post(
    "/sessions",
    status_code=status.HTTP_201_CREATED,
    response_model=SessionRead,
    summary="Book a session",
    description=(
        "Books one of a mentor's offerings at an instant that offering "
        "currently offers. The mentor follows from `session_type_id`; you are "
        "the mentee.\n\n"
        "**`starts_at` must match a slot from "
        "`GET /users/{mentor_id}/availability/slots` exactly**, to the second. "
        "That endpoint is the definition of what is bookable and this one asks "
        "it rather than reimplementing it — so the notice window, the mentor's "
        "hours or the offering's own scheduling windows, blocked dates and "
        "existing bookings all apply here, with no second set of rules to "
        "disagree with. Anything the grid does not offer is a `422`, whatever "
        "the reason, and the client's response to every one of them is the "
        "same: re-read `/slots`.\n\n"
        "**The status you get back is the mentor's setting, not your choice.** "
        "An offering that requires confirmation yields "
        "`pending_mentor_approval` and waits for them; one that does not yields "
        "`confirmed` immediately.\n\n"
        "**`Idempotency-Key` is required.** Send a fresh value per booking "
        "attempt and reuse it on every retry of that attempt: a retry replays "
        "the original response — the same session, the same `201` — instead of "
        "booking a second hour. A key is replayable for 24 hours and is scoped "
        "to you, so it can never collide with another caller's. Reusing one "
        "with a **different** body is a `422` rather than a silent replay of "
        "the wrong answer.\n\n"
        f"A replayed response carries `{REPLAYED_HEADER}: true`.\n\n"
        "**`meeting_url` is null on a new session.** It is generated per "
        "session when the session is confirmed — a static personal room means "
        "back-to-back sessions share it and an early joiner walks into the "
        "previous one."
    ),
    responses=BOOKING_RESPONSES,
)
async def book(booked: BookedSessionDep, response: Response) -> SessionRead:
    """The status code comes from the reservation, not from the decorator.

    They agree today — `201` is the only success booking has — and the plumbing
    is here because the *stored* code is what a replay must return. A replay
    answering `200` to a stored `201` would be a different answer to the same
    request, which is the one thing an idempotent endpoint may not do.

    The header is advisory and no client should need it, which is exactly why it
    is worth sending: it is how an operator reading a log tells "the mentee
    booked twice" from "the mentee's phone retried once".
    """
    body, status_code, replayed = booked
    response.status_code = status_code
    if replayed:
        response.headers[REPLAYED_HEADER] = "true"
    # **Set on a replay too.** The header names where the session is, and a retry
    # that replayed the original answer is pointing at the same one — withholding
    # it would make the second response less useful than the first for no reason.
    response.headers["Location"] = f"/api/v1/sessions/{body['id']}"
    return SessionRead.model_validate(body)


TRANSITION_RESPONSES: dict[int | str, dict[str, str]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "The bearer token is absent, malformed, expired or wrongly signed."
    },
    status.HTTP_404_NOT_FOUND: {
        "description": (
            "No such session, you are not part of it, **or this action is not "
            "yours to take** — a mentee has no accept, a mentor has no "
            "withdraw. All one answer, following the same rule that answers a "
            "non-admin with *no such endpoint*: the action's URL does not "
            "exist for you. You can still read the session, so nothing is "
            "hidden that you could otherwise see."
        )
    },
    status.HTTP_409_CONFLICT: {
        "description": (
            "You are the right party and the session is in the wrong state — "
            "already answered, already called off, or finished. The message "
            "names the state it is actually in."
        )
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "description": (
            "The body failed validation, or `reason_code` is not one your side "
            "of the session may give. The codes drive refund policy, so they "
            "are restricted per party rather than open."
        )
    },
}


@router.post(
    "/sessions/{session_id}/accept",
    summary="Accept a booking request",
    description=(
        "**The mentor's decision, and only the mentor's.** A mentee has no "
        "accept: the whole point of `pending_mentor_approval` is that somebody "
        "else has to answer it.\n\n"
        "Only from `pending_mentor_approval`. An already-confirmed session is a "
        "`409` rather than a silent success — a client that thinks it just "
        "confirmed something has been told something untrue.\n\n"
        "**No body.** Agreeing explains itself, and a reason field here would "
        "be one more thing to send and for policy to have to ignore.\n\n"
        "**`meeting_url` is filled in by this call**, for a venue the platform mints "
        "— a Daily room or a Meet link on the calendar event. It was null while "
        "nothing created one, which this description went on claiming after that "
        "stopped being true.\n\n"
        "It is **not** returned to either party before the join window. The link "
        "is handed over by `/join`, five minutes before the start, so that a "
        "press of Join is something the platform can record."
    ),
    responses=TRANSITION_RESPONSES,
)
async def accept_session(_: AcceptedSessionDep) -> dict[str, bool]:
    return {"accepted": True}


@router.post(
    "/sessions/{session_id}/decline",
    summary="Decline a booking request",
    description=(
        "**The mentor's other answer**, and the counterpart to `withdraw`: this "
        "is the mentor refusing a request, that is the mentee taking one back. "
        "Both act on a request nobody has agreed to yet, which is what "
        "separates them from `cancel`.\n\n"
        "A reason is optional. Better to give one, but requiring it turns a "
        "clear-cut decision into a form to argue with — and every migrated "
        "event carries none, because legacy held only free text."
    ),
    responses=TRANSITION_RESPONSES,
)
async def decline_session(_: DeclinedSessionDep) -> dict[str, bool]:
    return {"declined": True}


@router.post(
    "/sessions/{session_id}/withdraw",
    summary="Take back a booking request",
    description=(
        "**The mentee's, and only before the mentor answers.** Once a session "
        "is confirmed it is `cancel`, and the two are separate statuses "
        "deliberately: a request nobody accepted is not the same fact as a "
        "booking broken after agreement, and they carry different policy for "
        "refunds and for mentor-reliability statistics.\n\n"
        "The slot goes back on the grid immediately — nothing was agreed, so "
        "there is nothing to protect."
    ),
    responses=TRANSITION_RESPONSES,
)
async def withdraw_session(_: WithdrawnSessionDep) -> dict[str, bool]:
    return {"withdrawn": True}


@router.post(
    "/sessions/{session_id}/cancel",
    summary="Call off a confirmed session",
    description=(
        "**The one action either party may take.** The difference from "
        "`withdraw` is the state rather than the person: a confirmed session "
        "was agreed, so calling it off breaks an agreement whoever does it.\n\n"
        "**Refused within ten minutes of the start**, and after it. By then the "
        "other party is already on their way to the call, and what happened is "
        "an absence rather than a cancellation — which is the attendance "
        "sweep's answer to record, not this endpoint's.\n\n"
        "**The hour goes back on the grid**, unless the mentor says they are not "
        "free. It used to be kept indefinitely on the reasoning that a mentor who "
        "cancels is probably busy — which left hours hidden that nothing could "
        "ever release, and that `sessions_no_mentor_double_booking` would have "
        "accepted a booking for anyway.\n\n"
        "**`release_slot: false` writes an availability exception** for that time, "
        "which is a mentor-only answer — a mentee cancelling says nothing about "
        "the mentor's calendar. It is an ordinary block: visible beside any other, "
        "removable, and applying to every offering rather than this one. It says "
        "*unavailable*, not *reserved for somebody* — this endpoint has no way to "
        "hold time for a particular person.\n\n"
        "Which reason codes you may give depends on which side you are on. "
        "Sending one you may not is a `422` that names it, rather than a "
        "silently dropped field."
    ),
    responses=TRANSITION_RESPONSES,
)
async def cancel_session(_: CancelledSessionDep) -> dict[str, bool]:
    return {"cancelled": True}


@router.post(
    "/sessions/{session_id}/join",
    summary="Record that you arrived",
    description=(
        "Marks **you** present, from five minutes before the start to fifteen "
        "minutes after it. Either party calls it for themselves; neither can "
        "call it for the other, because attendance drives both parties' "
        "reliability figures and marking somebody present is editing their "
        "record.\n\n"
        "**Safe to call twice.** A dropped call or a refreshed tab is the "
        "ordinary case, and `joined_at` keeps the *first* arrival rather than "
        "the last press.\n\n"
        "Outside the window, or on a session that is not `confirmed`, this is a "
        "`409` rather than a quiet success — a client that believes it "
        "registered an arrival will not try again.\n\n"
        "**Attendance is what you tell us.** Nothing observes the meeting: the "
        "calendar integration is a write target and an on-demand free/busy "
        "read, and conference records are not reachable for an individual "
        "mentor's Workspace. This press is the signal.\n\n"
        "The session's own outcome — `completed` when both of you came, "
        "`no_show` when either did not — is decided once for both parties after "
        "the window shuts, not here.\n\n"
        "**`meeting_url` in the response is where to go, and for a Daily "
        "session it is not the URL on the session.** That room is private and "
        "refuses anybody without a token, so what comes back here carries one "
        "minted for you, for this session, expiring when it ends. It is "
        "returned and never stored — a token kept on the row would be a live "
        "credential sitting in every backup, outliving the session it "
        "opens.\n\n"
        "**It can be `null` on a success.** Your arrival is recorded either "
        "way; a null means the venue could not be reached or none is "
        "configured, which is a different thing from being refused entry and "
        "should be shown as such."
    ),
    responses=TRANSITION_RESPONSES,
)
async def join_session(door: JoinedSessionDep) -> dict[str, object]:
    """`meeting_url` is where to go, and it is **not** the stored one for Daily.

    A private room refuses anybody without a token, so the address on the
    session opens nothing. What comes back here carries a credential minted for
    this caller, for this session, expiring when it ends — which is why it is
    returned rather than stored, and why the response is the only place it
    exists.
    """
    return {"joined": True, "meeting_url": door}
