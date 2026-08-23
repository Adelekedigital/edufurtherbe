"""Writing and correcting a review, and finding out what may be reviewed.

Transport only. Every rule this surface enforces is in `domain/reviews.py` or
`review_eligibility.py`, which is what lets the same rules serve the request
producer in the phase after this one without a request to hang them on.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.api.deps import EditedReviewDep, ReviewableSessionsDep, WrittenReviewDep
from app.api.schemas.common import Page
from app.api.schemas.reviews import ReviewableSessionRead, ReviewRead

router = APIRouter(prefix="/api/v1", tags=["reviews"])

#: `POST`'s refusals. **Not shared with `PATCH`**, which can return neither of
#: the two problem types below — advertising a `type` a route cannot emit tells
#: a client to write a branch that never runs, and the generated documentation
#: is where they would read it.
WRITE_RESPONSES: dict[int | str, dict[str, str]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "The bearer token is absent, malformed, expired or wrongly signed."
    },
    status.HTTP_404_NOT_FOUND: {
        "description": (
            "No such session or review, **or it is not yours** — one answer for "
            "all of them. Distinguishing *absent* from *not yours* tells anyone "
            "who can enumerate ids which ones are real."
        )
    },
    status.HTTP_409_CONFLICT: {
        "description": (
            "The request is well formed and the rule refuses it. **Branch on "
            "`type`, not on the message:**\n\n"
            "- `/problems/review-already-exists` — this session already carries "
            "your review. Terminal; never retry.\n"
            "- `/problems/review-interval-not-elapsed` — you reviewed this "
            "offering recently. Retry once the window passes.\n\n"
            "`PATCH` also answers `409` once the ten-minute edit window has "
            "shut, which carries no type: there is only one way for an edit to "
            "be refused."
        )
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "description": "The body failed validation — a rating off its scale, or empty public text."
    },
}


#: `PATCH`'s refusals, which are a different set. There is exactly one way for an
#: edit to be refused on state — the window has shut — so it carries no problem
#: type: a type is a promise a client may branch on the value, and one value is
#: not a branch.
EDIT_RESPONSES: dict[int | str, dict[str, str]] = {
    status.HTTP_401_UNAUTHORIZED: WRITE_RESPONSES[status.HTTP_401_UNAUTHORIZED],
    status.HTTP_404_NOT_FOUND: {
        "description": (
            "No such review, **or it is not yours**, **or it has been withdrawn** "
            "— one answer for all three, because distinguishing them tells anyone "
            "who can enumerate ids which ones are real."
        )
    },
    status.HTTP_409_CONFLICT: {
        "description": "The ten-minute edit window has shut. The review stands as written."
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "description": (
            "A rating off its scale, or a field emptied that the column cannot "
            "empty — every field may be *omitted*, but only `private_review` may "
            "be sent as `null`."
        )
    },
}


@router.get(
    "/me/reviewable-sessions",
    response_model=Page[ReviewableSessionRead],
    summary="Sessions you may review right now",
    description=(
        "Completed sessions of yours that you have not reviewed and are not "
        "inside the interval for, newest first.\n\n"
        "**This is what makes a `409` from `POST /reviews` exceptional rather "
        "than routine.** Ask here first: an empty list means there is nothing "
        "to review, one entry means write straight away, and more than one "
        "means show the mentee which session they mean.\n\n"
        "Pass `mentor_id` to narrow it to one mentor, which is what a profile's "
        "Reviews tab wants.\n\n"
        "`next_cursor` is always `null`. A mentee has a handful of unreviewed "
        "sessions at most, so the list is returned whole; the envelope is here "
        "because ADR 0016 puts it on every list, and a bare array has nowhere "
        "to put pagination the day one is needed."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "description": "The bearer token is absent, malformed, expired or wrongly signed."
        }
    },
)
async def list_reviewable_sessions(
    sessions: ReviewableSessionsDep,
) -> Page[ReviewableSessionRead]:
    return Page(
        data=[ReviewableSessionRead.model_validate(row) for row in sessions], next_cursor=None
    )


@router.post(
    "/reviews",
    status_code=status.HTTP_201_CREATED,
    response_model=ReviewRead,
    summary="Review a completed session",
    description=(
        "Reviews one completed session of yours. **The mentor follows from the "
        "session** — you do not send them, and cannot review somebody who never "
        "mentored you.\n\n"
        "**One review per session, and one per offering per interval.** A "
        "second session with the same mentor for a *different* offering may be "
        "reviewed straight away: a mentor strong at CV review and weak at "
        "interview prep is two facts, and one window over both would keep "
        "whichever was booked first.\n\n"
        "**No `Idempotency-Key`.** The uniqueness rule is the guard: a retry "
        "that already succeeded comes back `409` with "
        "`/problems/review-already-exists`, which is a true answer rather than "
        "a second row. Booking needs a key because a replay must return the "
        "same session; here the second attempt has nothing new to say.\n\n"
        "**Ratings are words, not numbers.** `not_great`, `great`, `excellent` "
        "for the four mentor questions; `valuable_rating` is `1..5` and "
        "`nps_recommend_score` is `1..10`, both genuine point scales."
    ),
    responses=WRITE_RESPONSES,
)
async def write(review: WrittenReviewDep, response: Response) -> ReviewRead:
    response.headers["Location"] = f"/api/v1/reviews/{review['id']}"
    return ReviewRead.from_row(review)


@router.patch(
    "/reviews/{review_id}",
    response_model=ReviewRead,
    summary="Correct a review you just wrote",
    description=(
        "**A compose grace period, not an amendment.** Ten minutes from when "
        "the review was written, after which it stands — a mentor's profile is "
        "a dated list, and a review that can be rewritten later is a history "
        "that rewrites itself.\n\n"
        "Author only. Every content field may be corrected; **`session_id` "
        "cannot** — it is what the review is *about*, and changing it would "
        "step around the eligibility rules rather than fix a typo.\n\n"
        "The interval always runs from when the review was first written, so "
        "an edit never postpones the next one.\n\n"
        "Send only the fields you are changing. Sending `private_review: null` "
        "clears it; leaving it out keeps it."
    ),
    responses=EDIT_RESPONSES,
)
async def edit(review: EditedReviewDep) -> ReviewRead:
    return ReviewRead.from_row(review)
