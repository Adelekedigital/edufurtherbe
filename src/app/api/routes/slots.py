"""Bookable slots — the one endpoint in this codebase that takes no token.

**Its own module because of settled decision #64**, which says a non-catalogue
public endpoint carries the `public` tag while everything else takes its domain
name. A `tags=["public"]` route sitting among the authenticated availability
routes would carry both tags and read as an exception somebody forgot to
justify; a separate router makes the split visible in the file list, which is
where a reviewer looks first.

The path still lives under `/users/{user_id}/availability/` — a mentor's
bookable time *is* their availability, and moving the URL to match the module
would have split one resource across two prefixes for the sake of tidiness.

**There is no viewer here.** Every other read in `api/` resolves a caller and
scopes to them. This one resolves nobody, and what stands in its place is the
mentor's own state — approved *and* listed, checked inside the query in
`infra/db/slot_store.py`. The absence of `CurrentUserDep` below is the entire
authorization decision, which is why it is stated rather than left to be noticed.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import SlotsDep
from app.api.schemas.common import Page
from app.api.schemas.slots import SlotRead

router = APIRouter(prefix="/api/v1/users/{user_id}/availability", tags=["public"])


#: The public read. **No 401**, because there is no token to be wrong — and the
#: 404 means something different here: not "not yours" but "no such publicly
#: bookable offering", which covers an unapproved mentor, an unlisted one, an
#: inactive or deleted session type, and one belonging to somebody else.
PUBLIC_RESPONSES: dict[int | str, dict[str, str]] = {
    status.HTTP_404_NOT_FOUND: {
        "description": (
            "No such mentor, session type, or the mentor is not publicly "
            "bookable. Indistinguishable on purpose — telling them apart would "
            "say which mentors exist and what state they are in."
        )
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "description": "`end` is not after `start`, or the range is longer than 56 days."
    },
}


@router.get(
    "/slots",
    response_model=Page[SlotRead],
    summary="Bookable slots for one session type",
    description=(
        "When this mentor could actually take a session of this type — their "
        "declared availability, minus what is booked, minus anything too soon "
        "to book, sliced into spans of the offering's duration.\n\n"
        "**Public.** No token is required. A mentor is here only while they are "
        "both approved and listed; pausing removes them from this endpoint as "
        "well as from search.\n\n"
        "**`start` and `end` are days in the *mentor's* timezone**, and `end` is "
        "exclusive. A recurring window is declared in wall-clock time, so the "
        "day it belongs to is the mentor's day — a client sending its own local "
        "dates gets the edges wrong by one.\n\n"
        "**Both are optional.** `start` defaults to *the mentor's* today and "
        "`end` to a week after it. The default cannot be the caller's today: "
        "this endpoint takes no token, so there is no profile to read a zone "
        "from, and IP or `Accept-Language` guess wrong for anyone travelling or "
        "behind a VPN. A client that means its own today sends `start`.\n\n"
        "`session_type_id` stays required — a slot's length and notice window "
        "come from the offering, so there is no such thing as a slot without "
        "one.\n\n"
        "**The first slots you might expect can be missing.** Each offering "
        "carries `min_notice_minutes` and anything starting sooner than that is "
        "not offered. The platform floor is **24 hours** — legacy allowed no "
        "same-day booking — so by default nothing within a day of now is "
        "bookable. Nothing in the past is offered either, whatever the range "
        "asked for.\n\n"
        "Slots start at the mentor's window and step by the session duration, "
        "so a 09:00-12:00 window at 45 minutes gives 09:00, 09:45, 10:30 and "
        "11:15 — never 09:15. A slot that would run past the end of a window is "
        "not offered at all.\n\n"
        "`next_cursor` is always `null`. The range is capped, so the answer is "
        "returned whole; the envelope is here because ADR 0016 puts it on every "
        "list.\n\n"
        "**A slot is not a reservation.** Two people can see the same one, and "
        "the second booking is refused by the database. Anything this endpoint "
        "promised would be stale before it reached a browser."
    ),
    responses=PUBLIC_RESPONSES,
)
async def list_bookable_slots(slots: SlotsDep) -> Page[SlotRead]:
    return Page(data=[SlotRead.from_interval(slot) for slot in slots], next_cursor=None)
