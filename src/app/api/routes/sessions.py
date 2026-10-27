"""Sessions a user is a party to, what happened to each one, and the five writes.

Booking, then the four transitions — accept, decline, withdraw, cancel. Whether
a transition refunds the mentee is decision 229, decided in `domain/refunds.py`
from who acted and when — never from `reason_code`, which is for reporting.

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

from uuid import UUID

from fastapi import APIRouter, Response, status

from app.api.deps import (
    AcceptedSessionDep,
    BookedSessionDep,
    CancelledSessionDep,
    DeclinedSessionDep,
    JoinedSessionDep,
    JoinLeadDep,
    MovedSessionDep,
    RefundPolicyDep,
    RemindedSessionDep,
    SentMissedReasonDep,
    SessionAnswersDep,
    SessionDetailDep,
    SessionDoorDep,
    SessionEventsDep,
    SessionsPageDep,
    WithdrawnSessionDep,
)
from app.api.schemas.common import Page
from app.api.schemas.intake import SessionAnswerRead
from app.api.schemas.sessions import (
    DoorRead,
    JoinRead,
    MissedReasonRead,
    SessionAcceptedRead,
    SessionCancelledRead,
    SessionDeclinedRead,
    SessionEventRead,
    SessionRead,
    SessionWithdrawnRead,
)
from app.domain.enums import MissedReason, SessionReasonCode
from app.domain.missed_reasons import NOTE_MAX_LENGTH
from app.domain.request_reminders import REMINDER_MESSAGE_MAX_LENGTH
from app.domain.sessions import TRANSITIONS

router = APIRouter(prefix="/api/v1", tags=["sessions"])


def reason_codes_doc(action: str, *, offers: bool) -> str:
    """The reason codes each side may give for ``action``, as published.

    **Generated from the domain's table**, never written out, so the list a
    client reads cannot drift from the one the server enforces. Listed in the
    enum's order, which puts `other` last. ``offers`` is whether the action's
    body carries `suggested_starts_at`, which lifts the requirement.
    """
    sides = "\n".join(
        f"- {role.value}: " + ", ".join(f"`{code}`" for code in SessionReasonCode if code in codes)
        for role, codes in TRANSITIONS[action].reasons.items()
    )
    unless = ", unless a mentor offers another time with `suggested_starts_at`" if offers else ""
    return (
        f"**`reason_code` is required**{unless}. The codes each side may give:\n\n"
        f"{sides}\n\n"
        "Any other code, or none where one is required, is a `422`."
    )


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
        "description": (
            "The `cursor` was not one this endpoint issued or was issued for the "
            "other `order`, `to` is not after `from`, `order` is not `asc` or "
            "`desc`, a `status` is not a session status, or `as` is not `mentor` "
            "or `mentee`."
        )
    },
}


@router.get(
    "/users/{user_id}/sessions",
    response_model=Page[SessionRead],
    summary="List a user's sessions",
    description=(
        "Every session this user is a party to — as mentor, as mentee, or "
        "both — newest first by default. **`as=mentor`** or **`as=mentee`** "
        "narrows it to one side, in the paged query, so a mentor's next session "
        "is one call.\n\n"
        "**`order=asc`** lists soonest first instead, for an Upcoming view: "
        "pair it with `status=confirmed&from=<today>` and the next session leads "
        "page one. The order applies before the page is cut, and a `cursor` "
        "keeps the order it was issued under — replayed with the other order it "
        "is a `422`.\n\n"
        "A user with no sessions gets an empty page and a `200`, not a `404`: "
        "an empty collection exists, it simply has nothing in it, and a `404` "
        "would leave a client unable to tell that from being refused.\n\n"
        "**Cancelled and missed sessions are included.** They are still that "
        "person's history, and omitting them would silently shorten it.\n\n"
        "Each row carries `mentor_id` and `mentee_id`, so a client can tell "
        "which side of the session this user was on without a second call.\n\n"
        "**Optional filters** for a month view: `from` (inclusive) and `to` "
        "(exclusive) are calendar dates in the **caller's** time zone, matched "
        "on the session's start; `status` repeats "
        "(`status=confirmed&status=pending_mentor_approval`). `order` and "
        "`cursor` paging apply within the filter."
    ),
    responses=LIST_RESPONSES,
)
async def list_user_sessions(
    page: SessionsPageDep, lead: JoinLeadDep, refunds: RefundPolicyDep
) -> Page[SessionRead]:
    rows, next_cursor = page
    return Page(
        data=[SessionRead.from_row(row, opens_before=lead, refunds=refunds) for row in rows],
        next_cursor=next_cursor,
    )


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
async def read_session(
    session: SessionDetailDep, lead: JoinLeadDep, refunds: RefundPolicyDep
) -> SessionRead:
    return SessionRead.from_row(session, opens_before=lead, refunds=refunds)


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
        "`reason_code` is null where nothing was coded (see the field), "
        "including every migrated event: the legacy app held only free text, "
        "so there is nothing to code them from."
    ),
    responses=READ_RESPONSES,
)
async def read_session_events(events: SessionEventsDep) -> Page[SessionEventRead]:
    return Page(data=[SessionEventRead.from_row(row) for row in events], next_cursor=None)


@router.get(
    "/sessions/{session_id}/answers",
    response_model=Page[SessionAnswerRead],
    summary="Read the answers given when booking",
    description=(
        "The intake form's answers for this booking, one entry per **answered** "
        "question in the form's order, for the mentor to prepare with. An "
        "optional question the mentee skipped is not listed.\n\n"
        "**Readable by the session's mentee, its mentor, and admins**; anyone "
        "else gets the same `404` as a session that does not exist.\n\n"
        "**An empty list is not an error**: a booking made on an offering with "
        "no form, or migrated from the old app, was asked nothing.\n\n"
        "A file answer carries the file's `id`; download it with "
        "`GET /api/v1/intake-files/{id}`, which admits the same three readers. "
        "`available: false` means retention has removed the file.\n\n"
        "**Questions read as the mentee saw them.** The wording of each question "
        "and chosen option is kept at booking, so a question reworded afterwards "
        "still shows the words that were answered. Answers given before that copy "
        "was kept (2026-10-04) show the current wording. A removed question is "
        "still listed, with `retired: true`.\n\n"
        "`next_cursor` is always `null`: a form has at most a handful of "
        "questions and is returned whole."
    ),
    responses=READ_RESPONSES,
)
async def read_session_answers(answers: SessionAnswersDep) -> Page[SessionAnswerRead]:
    return Page(data=[SessionAnswerRead.from_row(row) for row in answers], next_cursor=None)


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
            "and try again.\n\n"
            "**Unfinished onboarding is checked first** (decision 272): "
            "`/problems/booking-needs-onboarding`, with `missing` naming the "
            "answers still owed (empty when only finishing is left). Send the "
            "mentee to finish setting up, then retry the same request with the "
            "same key: nothing was stored against it.\n\n"
            "**Insufficient credit is a third, and it does not pass.** It "
            "carries the problem type `/problems/insufficient-credit`, and the "
            "answer is to wait for the monthly grant or invite somebody — never "
            "to retry the same request.\n\n"
            "**Your booking limits** (settled decision 231) are three more, each "
            "with its own problem type, checked in this order:\n"
            "- `/problems/booking-overlap`: you already have a session at an "
            "overlapping time, with any mentor.\n"
            "- `/problems/booking-with-mentor-exists`: you already have a session "
            "pending or coming up with this mentor.\n"
            "- `/problems/booking-limit-reached`: you already have 2 sessions "
            "pending or coming up.\n\n"
            "A session stops counting once it is completed, cancelled, declined, "
            "expired, withdrawn or missed. Suggested copy, which never points to "
            'cancelling: *"You can book another once one of them has taken '
            'place."* and *"You can book {mentor} again after it."*'
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

#: The move's own responses (decision 300). **Not `BOOKING_RESPONSES`**: a move
#: spends no credit, so it can never answer `/problems/insufficient-credit`, and
#: its `404` is about the session moved rather than an offering.
MOVE_RESPONSES: dict[int | str, dict[str, str]] = {
    status.HTTP_401_UNAUTHORIZED: BOOKING_RESPONSES[status.HTTP_401_UNAUTHORIZED],
    status.HTTP_404_NOT_FOUND: {
        "description": (
            "No such session, or you are not its mentee. The mentor gets this "
            "too: moving is the mentee's action."
        )
    },
    status.HTTP_409_CONFLICT: {
        "description": (
            "Nothing changed; the old session is as it was.\n\n"
            "- `/problems/move-too-late`: a confirmed session past its "
            "`refund_until`. It can only be cancelled.\n"
            "- The session is not live (already ended), has no session type (a "
            "migrated session), the time was taken while you were moving, or a "
            "request with this `Idempotency-Key` is still in flight.\n"
            "- `/problems/booking-needs-onboarding`, `/problems/booking-overlap`, "
            "`/problems/booking-with-mentor-exists` and "
            "`/problems/booking-limit-reached`, as for booking.\n\n"
            "Never `/problems/insufficient-credit`: the credit that paid for the "
            "old session pays for the new one."
        )
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "description": (
            "The body failed validation (`message` over 2000 characters), the "
            "instant is not one the session's offering offers now, or this "
            "`Idempotency-Key` already stands for a different request."
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
        "`confirmed` immediately. **One exception:** the time a mentor offered "
        "you when declining your request is approved in advance, so booking "
        "exactly that time, at that offering, is `confirmed` immediately, even "
        "after its hold has lapsed.\n\n"
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
        "previous one.\n\n"
        "**Booking a suggested time** (#339) is this same call, at the "
        "`suggestion.starts_at` of the declined or cancelled session, with its "
        "`session_type`. While the suggestion is `active` the time is held for "
        "you alone: `/slots` hides it from everyone else and shows it open to you "
        "(with your token) while held, and another mentee booking it is refused — a `422` if "
        "the grid was read after the offer, a `409` if the two raced. Booking it "
        "marks the suggestion `booked`. Answers are required as for any booking."
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


@router.post(
    "/sessions/{session_id}/move",
    status_code=status.HTTP_201_CREATED,
    response_model=SessionRead,
    summary="Move your session to another time",
    description=(
        "The mentee moves their session in one call (decision 300): the old one "
        "ends and the new time is booked in **one transaction**, so any refusal "
        "leaves the old session exactly as it was.\n\n"
        "- A `confirmed` session ends `cancelled`, and moves **only until its "
        "`refund_until`**, while cancelling would still refund the credit; after "
        "that it is `409` `/problems/move-too-late` and can only be cancelled. "
        "A `pending_mentor_approval` one ends `withdrawn` and can always move. "
        "Its history says `rescheduled`.\n"
        "- **The session type stays the session's own**; another type is a new "
        "booking, since its form asks different questions.\n"
        "- **The new time is booked as `POST /sessions` books it**: it must be a "
        "slot `/slots` offers, and the mentor's confirmation setting decides "
        "whether it is `confirmed` or `pending_mentor_approval`. The old time is "
        "released either way, never held.\n"
        "- **The credit moves**: no refund, no second charge, so a move works at "
        "a zero balance. A later refund of the new session pays it back once.\n"
        "- **The intake moves**: the answers and files carry over; send none.\n"
        "- **One email**, the one booking sends: `session_booked`, or "
        "`session_requested` when the mentor must answer. Nothing is sent about "
        "the old session, and nothing about credits.\n\n"
        "`Idempotency-Key` is required, as for booking; a retry replays the "
        f"first answer with `{REPLAYED_HEADER}: true`.\n\n"
        "Refusals: `404` not your session as its mentee; `409` it is not live, "
        "it has no session type (a migrated session), or the time was taken "
        "while moving; `409` `/problems/move-too-late` as above; `422` the time "
        "is not offered; `/problems/booking-overlap`, "
        "`/problems/booking-with-mentor-exists` and "
        "`/problems/booking-limit-reached` as for booking."
    ),
    responses=MOVE_RESPONSES,
)
async def move_session(moved: MovedSessionDep, response: Response) -> SessionRead:
    """The new session, with the status code the reservation stored."""
    body, status_code, replayed = moved
    response.status_code = status_code
    if replayed:
        response.headers[REPLAYED_HEADER] = "true"
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
            "The body failed validation, or `reason_code` is missing where one "
            "is required or is not one your side of the session may give; "
            "both point at `/reason_code`. Each side reports with its own "
            "codes, so they are restricted per party; a code never decides a "
            "refund."
        )
    },
}

#: `/door`'s own responses. **Not `TRANSITION_RESPONSES`**, which it borrowed
#: until a review pointed out the published contract then told the frontend this
#: bodyless endpoint could `422` on a bad `reason_code` and `404` for "an action
#: not yours to take" — neither of which it can do. The frontend generates its
#: error handling from this document, so a wrong entry here is a wrong branch
#: there.
DOOR_RESPONSES: dict[int | str, dict[str, str]] = {
    status.HTTP_401_UNAUTHORIZED: TRANSITION_RESPONSES[status.HTTP_401_UNAUTHORIZED],
    status.HTTP_404_NOT_FOUND: {
        "description": (
            "No such session, or you are not part of it — one answer, so a "
            "stranger cannot learn that a session exists. Never a credential."
        )
    },
    status.HTTP_409_CONFLICT: {
        "description": (
            "Your session, but there is no room to enter: it was never agreed "
            "to or was called off, or it is outside the span from `join_opens_at` "
            "to its end. The message names which.\n\n"
            "**`type: /problems/join-window-closed`** when arrivals have "
            "stopped and you never pressed Join in time: once `join_closes_at` "
            "passes, the door is for coming back, not for arriving late."
        )
    },
}


@router.post(
    "/sessions/{session_id}/accept",
    response_model=SessionAcceptedRead,
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
        "**`meeting_url` is filled in by this call for a Daily room.** A Google "
        "Meet session's link is added later, five minutes before the start or "
        "at the first press of Join, whichever comes first, so it stays off "
        "both calendars until then.\n\n"
        "It is **not** returned to either party before the join window. The link "
        "is handed over by `/join`, from `join_opens_at`, so that a "
        "press of Join is something the platform can record."
    ),
    responses=TRANSITION_RESPONSES,
)
async def accept_session(_: AcceptedSessionDep) -> SessionAcceptedRead:
    return SessionAcceptedRead(accepted=True)


@router.post(
    "/sessions/{session_id}/decline",
    response_model=SessionDeclinedRead,
    summary="Decline a booking request",
    description=(
        "**The mentor's other answer**, and the counterpart to `withdraw`: this "
        "is the mentor refusing a request, that is the mentee taking one back. "
        "Both act on a request nobody has agreed to yet, which is what "
        "separates them from `cancel`.\n\n"
        "**`suggested_starts_at` offers another time instead** (#339): see the "
        "field. The request still ends `declined` and the credit still comes "
        "back; the offer is separate, held for the mentee for two hours. "
        "**The offered time is approved in advance**: when the mentee books it, "
        "the session is `confirmed` at once, without waiting for you again, "
        "and that lasts past the hold while the time is still free.\n\n"
        + reason_codes_doc("decline", offers=True)
    ),
    responses=TRANSITION_RESPONSES,
)
async def decline_session(_: DeclinedSessionDep) -> SessionDeclinedRead:
    return SessionDeclinedRead(declined=True)


@router.post(
    "/sessions/{session_id}/withdraw",
    response_model=SessionWithdrawnRead,
    summary="Take back a booking request",
    description=(
        "**The mentee's, and only before the mentor answers.** Once a session "
        "is confirmed it is `cancel`, and the two are separate statuses "
        "deliberately: a request nobody accepted is not the same fact as a "
        "booking broken after agreement, and mentor-reliability statistics "
        "count them apart. A withdrawn request always returns your credit.\n\n"
        "The slot goes back on the grid immediately — nothing was agreed, so "
        "there is nothing to protect.\n\n" + reason_codes_doc("withdraw", offers=False)
    ),
    responses=TRANSITION_RESPONSES,
)
async def withdraw_session(_: WithdrawnSessionDep) -> SessionWithdrawnRead:
    return SessionWithdrawnRead(withdrawn=True)


@router.post(
    "/sessions/{session_id}/cancel",
    response_model=SessionCancelledRead,
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
        "**The mentee's credit:** a **mentor** cancelling always refunds it, "
        "whatever reason is given. A **mentee** cancelling gets it back up to "
        "the session's `refund_until` (`/me.mentee_cancel_refund_hours` before "
        "the start), the boundary included; after that the credit is used. A "
        "refund is a new `refund` lot: it never expires if the credit it "
        "replaces never did, and otherwise expires at the end of the current "
        "month (UTC), whatever date the original carried.\n\n"
        "**A mentor may offer another time instead** with `suggested_starts_at` "
        "(#339): the session still ends `cancelled`, refunded as above, and the "
        "offered time is held for the mentee for two hours. Unlike a decline's "
        "offer it is not approved in advance: booking it follows the mentor's "
        "usual approval setting.\n\n"
        + reason_codes_doc("cancel", offers=True)
        + " The code does not decide the refund."
    ),
    responses=TRANSITION_RESPONSES,
)
async def cancel_session(_: CancelledSessionDep) -> SessionCancelledRead:
    return SessionCancelledRead(cancelled=True)


@router.post(
    "/sessions/{session_id}/join",
    summary="Record that you arrived",
    description=(
        "Records that **you** pressed Join, from `join_opens_at` (ten minutes before the "
        "start unless configured otherwise) to fifteen "
        "minutes after it — or to the session's end, if it is shorter than "
        "that: nobody joins a session that is over. Read "
        "`join_closes_at` rather than computing it. Either party calls it for "
        "themselves; neither can "
        "call it for the other, because attendance drives both parties' "
        "reliability figures and marking somebody present is editing their "
        "record.\n\n"
        "**Safe to call twice.** A dropped call or a refreshed tab is the "
        "ordinary case, and `joined_at` keeps the *first* arrival rather than "
        "the last press.\n\n"
        "Outside the window, or on a session that is not `confirmed`, this is a "
        "`409` rather than a quiet success — a client that believes it "
        "registered an arrival will not try again.\n\n"
        "**What the press decides depends on the venue** (#382). For an "
        "EduFurther video (Daily) session it sets `joined_at`, which is what "
        "lets you back in after arrivals stop, but **not attendance**: you are "
        "`attended` once Daily sees you in the room, and pressing Join without "
        "entering is not attending. For Google Meet and custom venues nothing "
        "observes the room, so the press is the signal and marks you "
        "`attended`.\n\n"
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
    response_model=JoinRead,
    responses=TRANSITION_RESPONSES,
)
async def join_session(door: JoinedSessionDep) -> JoinRead:
    """`meeting_url` is where to go, and it is **not** the stored one for Daily.

    A private room refuses anybody without a token, so the address on the
    session opens nothing. What comes back here carries a credential minted for
    this caller, for this session, expiring when it ends — which is why it is
    returned rather than stored, and why the response is the only place it
    exists.
    """
    return JoinRead(joined=True, meeting_url=door)


@router.post(
    "/sessions/{session_id}/door",
    summary="Get back into a session that is running",
    description=(
        "Your way into the room, **without recording an arrival** — for after "
        "a dropped call or a refreshed tab.\n\n"
        "**Open from `join_opens_at` until the session ends**, "
        "which is never earlier than `/join` closes: arrivals stop fifteen "
        "minutes in, or at the end of a shorter session. The difference is "
        "deliberate: `/join` closes when the session's outcome becomes "
        "decidable, and an arrival recorded later would change a verdict "
        "already reached. Getting *back* in has no such constraint, so a party "
        "who pressed Join and drops at minute twenty of an hour can still return. On a session "
        "of fifteen minutes or less the two close together.\n\n"
        "**Call `/join` to arrive and this to re-enter.** Calling this records "
        "nothing: `joined_at` and `attendance_status` are untouched. That is "
        "deliberate: pressing Join is the arrival, and this is a door.\n\n"
        "**After `join_closes_at` it admits only a party who pressed Join in "
        "time** (owner, 2026-10-08). Anyone else gets `409` with `type: "
        "/problems/join-window-closed`: there are no late first-timers. Before "
        "then, any party of the session gets a door.\n\n"
        "**`meeting_url` is minted for you and never stored**, exactly as on "
        "`/join`: for a Daily session it carries a token for this caller, "
        "expiring when the session ends, and the mentor's carries owner rights. "
        "For any other venue it is the session's stored address; a Google Meet "
        "session gets its link here if it has none yet.\n\n"
        "**It can be `null` on a success**, meaning the venue could not be "
        "reached or none is configured — a different thing from being refused, "
        "and worth showing as such.\n\n"
        "**A settled session still has a door.** Fifteen minutes in, the "
        "session's status moves to `completed` or `no_show` — the outcome is "
        "decided — but the session is still running, so `/door` keeps working "
        "until it ends for a party who pressed Join in time. Do not hide Rejoin "
        "because `status` stopped being "
        "`confirmed`.\n\n"
        "Outside that span, or on a session never agreed to or called off — "
        "pending, declined, withdrawn, expired or cancelled — this is a `409`. "
        "A session that is not yours is a `404`, indistinguishable from one "
        "that does not exist."
    ),
    response_model=DoorRead,
    responses=DOOR_RESPONSES,
)
async def enter_session(door: SessionDoorDep) -> DoorRead:
    """`/join`'s door without its claim that you joined — this endpoint makes no
    such claim, and the response schema leaves no field to read one into."""
    return DoorRead(meeting_url=door)


MISSED_REASON_RESPONSES: dict[int | str, dict[str, str]] = {
    **READ_RESPONSES,
    status.HTTP_403_FORBIDDEN: {
        "description": (
            "`/problems/missed-reason-not-yours`: the other party missed this "
            "session and you did not (your attendance is not `no_show`). Only a "
            "party gets this, so it discloses nothing."
        )
    },
    status.HTTP_409_CONFLICT: {
        "description": (
            "`/problems/missed-reason-already-sent`: you already said why. "
            "One reason per person per session."
        )
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "description": (
            "`/problems/session-not-missed`: nobody is recorded as missing this "
            "session. Either it is not settled yet (its status is not "
            "`completed` or `no_show`), or it is and neither party's attendance "
            "is recorded as `no_show`. Checked before the `403`. Otherwise the "
            f"request itself: an unknown `reason`, a `note` over {NOTE_MAX_LENGTH} "
            "characters, or a field this endpoint does not take."
        )
    },
}


@router.post(
    "/sessions/{session_id}/missed-reason",
    status_code=status.HTTP_201_CREATED,
    response_model=MissedReasonRead,
    summary="Say why you missed a session",
    description=(
        "For a party whose own attendance is `no_show` on a session settled as "
        "`completed` or `no_show`. **Internal**: EduFurther's admins read it; "
        "the other party never sees it, through this or any endpoint. If both "
        "of you missed it, each may send their own.\n\n"
        f"`reason` is one of {', '.join(f'`{m.value}`' for m in MissedReason)}. "
        f"`note` is optional, at most {NOTE_MAX_LENGTH} characters, trimmed, and "
        "a blank one is stored as `null`.\n\n"
        "**Once each.** A second send is a `409`, which is also what a retry of "
        "a send that succeeded gets, so no `Idempotency-Key` is needed. "
        "`missed_reason_sent` on the session then reads `true` for you, and "
        "`Location` names that session: the reason has no URL of its own."
    ),
    responses=MISSED_REASON_RESPONSES,
)
async def send_missed_reason(
    session_id: UUID, reason: SentMissedReasonDep, response: Response
) -> MissedReasonRead:
    # The reason has no URL of its own; the session is where it shows, as
    # `missed_reason_sent`.
    response.headers["Location"] = f"/api/v1/sessions/{session_id}"
    return MissedReasonRead(**reason)


REMINDER_RESPONSES: dict[int | str, dict[str, str]] = {
    **READ_RESPONSES,
    status.HTTP_403_FORBIDDEN: {
        "description": (
            "`/problems/reminder-mentee-only`: you are the mentor. Only the "
            "mentee who made the request may remind. Only a party gets this, so "
            "it discloses nothing."
        )
    },
    status.HTTP_409_CONFLICT: {
        "description": (
            "`/problems/request-not-pending`: the request no longer awaits the "
            "mentor's answer (answered, withdrawn, expired, or past "
            "`respond_by`, or past `starts_at` when there is no `respond_by`). "
            "`/problems/reminder-already-sent`: you already reminded the mentor "
            "about this request; once per session. When both apply, it is "
            "`request-not-pending`."
        )
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "description": (
            f"`message` is missing, blank, over {REMINDER_MESSAGE_MAX_LENGTH} "
            "characters after trimming, or the body has a field this endpoint "
            "does not take."
        )
    },
}


@router.post(
    "/sessions/{session_id}/reminder",
    response_model=SessionRead,
    summary="Remind the mentor about your pending request",
    description=(
        "For the **mentee** of a request still awaiting the mentor's answer "
        "(`pending_mentor_approval`, before its deadline: `respond_by`, or "
        "`starts_at` when there is none). Emails the mentor "
        "your `message`, the requested time in their zone, how long they have "
        "left to answer, and a link to the request.\n\n"
        "**Once per session.** A second send is a `409`, which is also what a "
        "retry of a send that succeeded gets, so no `Idempotency-Key` is "
        "needed. Two at once send one email. Returns the session, with "
        "`reminder_sent_at` set."
    ),
    responses=REMINDER_RESPONSES,
)
async def remind_mentor(
    session: RemindedSessionDep, lead: JoinLeadDep, refunds: RefundPolicyDep
) -> SessionRead:
    return SessionRead.from_row(session, opens_before=lead, refunds=refunds)
