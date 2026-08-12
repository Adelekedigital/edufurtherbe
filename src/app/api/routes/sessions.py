"""Sessions a user is a party to, and what happened to each one.

**Read-only.** Booking, confirming and cancelling need the cancellation policy,
which project conventions record as deliberately undecided — publishing a write
contract now would encode a rule nobody has taken, and a contract is the
expensive thing to get wrong.

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

from fastapi import APIRouter, status

from app.api.deps import SessionDetailDep, SessionEventsDep, SessionsPageDep
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
