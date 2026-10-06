"""Being told when something ships (`/me/interest`).

**Feature-agnostic on purpose** (#365). Two *Notify me* controls had been cut
for want of somewhere to record a press — the Payments row on the mentor
Integrations page, and Explore's no-mentors state — so this records interest in
anything by an open slug rather than in one named feature by an enum. The next
coming-soon control anywhere in the product works with no backend release.

**Recording is feature-agnostic; notifying is not.** Sending anything for a new
key needs a template id and a `Notification` member, so a button built on this
may promise *"we'll let you know"* and never a date. Nothing here sends, and
nothing here is a queue anybody is working through.

**Any signed-in account**, not mentors only: Explore's case is a mentee's.
Guests cannot be told and so cannot register.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import OwnInterestsDep, RegisteredInterestDep, WithdrawnInterestDep
from app.api.schemas.common import Page
from app.api.schemas.interest import MAX_INTERESTS, InterestRead

router = APIRouter(prefix="/api/v1/me/interest", tags=["interest"])

RESPONSES: dict[int | str, dict[str, str]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "The bearer token is absent, malformed, expired or wrongly signed."
    },
}


@router.get(
    "",
    response_model=Page[InterestRead],
    summary="What you are waiting to hear about",
    description=(
        "Everything you have asked to be told about, oldest first. An empty "
        "`data` means you have asked for nothing — **never a `404`**, because "
        "having asked for nothing is not an error and a client renders the "
        "button either way.\n\n"
        "Read this to decide whether a *Notify me* control should already say "
        "*we'll let you know*: that is the whole reason it is readable rather "
        "than write-only.\n\n"
        f"`next_cursor` is always `null`. A per-account cap of about "
        f"{MAX_INTERESTS} bounds this, so the answer is returned whole; the "
        "envelope is here because ADR 0016 puts it on every list.\n\n"
        "*About*, not exactly: the cap is enforced by a count taken inside the "
        "writing transaction, so simultaneous requests for different features "
        "can each pass it. The ceiling is the cap plus one burst, never "
        "unbounded — stated loosely on purpose, because a client should not "
        "build on a number this cannot promise."
    ),
    responses=RESPONSES,
)
async def list_own_interests(waiting: OwnInterestsDep) -> Page[InterestRead]:
    return Page(data=[InterestRead.from_row(row) for row in waiting], next_cursor=None)


@router.post(
    "",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Ask to be told when something ships",
    description=(
        "**Pressing twice is pressing once.** A repeat answers `204` and "
        "changes nothing, including `registered_at` — so no `Idempotency-Key` "
        "is needed, and a client may send it on every click without "
        "remembering whether it already did.\n\n"
        "What this promises is that you are recorded, and nothing more. There "
        "is no date, and sending a message for a key needs work on our side "
        "that is separate from recording it — so copy built on this should say "
        "*we'll let you know* and never when."
    ),
    responses=RESPONSES
    | {
        status.HTTP_409_CONFLICT: {
            "description": (
                f"You already wait for {MAX_INTERESTS} features. A bound on "
                "rows rather than a product rule — withdraw one to add another. "
                "Nobody reaches this by using the product."
            )
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": (
                "`feature` is not a lowercase slug of 2 to 40 characters. The "
                "vocabulary is open, so this is the *only* check: a well-formed "
                "key naming nothing that exists is accepted, which is a button "
                "shipping before its backend rather than an error."
            )
        },
    },
)
async def register_own_interest(_: RegisteredInterestDep) -> None:
    return None


@router.delete(
    "/{feature}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Stop waiting to hear about something",
    description=(
        "Removes the record, so the control goes back to offering rather than "
        "confirming. Asking again later is a fresh registration.\n\n"
        "`404` if you were not waiting for it — **stated rather than treated as "
        "an idempotent success**, the same rule as disconnecting a calendar: "
        "somebody who believes they turned something off needs to know if they "
        "did not. A key that could never be stored answers `404` too, because "
        "absent is the truthful answer to it."
    ),
    responses=RESPONSES
    | {
        status.HTTP_404_NOT_FOUND: {"description": "You are not waiting for that feature."},
    },
)
async def withdraw_own_interest(_: WithdrawnInterestDep) -> None:
    return None
