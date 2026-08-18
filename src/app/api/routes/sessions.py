"""Sessions a user is a party to, what happened to each one, and booking one.

**One write, and it is the only one that needs no undecided rule.** Confirming
and cancelling wait on the cancellation policy, which project conventions record
as deliberately undecided; publishing a contract for those now would encode a
rule nobody has taken. Creating a session asks a question already answered —
what may be booked is whatever `/slots` currently offers, to the second.

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
    BookedSessionDep,
    SessionDetailDep,
    SessionEventsDep,
    SessionsPageDep,
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
    return SessionRead.model_validate(body)
