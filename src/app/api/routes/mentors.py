"""The public mentor profile — who a mentee is choosing between.

**Its own prefix, not `/users/{id}/…`.** That path already carries the
owner-and-admin view of a mentor profile, and a public variant one typo away
from it is the kind of adjacency that gets confused in a hurry. `/mentors/{…}`
also gives discovery its natural home when it arrives, as `GET /mentors`.

**`tags=["public"]`** per settled decision #64, joining `/slots` and
`/session-types`. Three public reads now, one visibility predicate between them.

D20's rule was three clauses — listed, *or* the viewer has a session, *or* the
viewer is an admin. Only the first survives here. A mentee with a session sees
*that session*, which carries the mentor's name since the party identity change;
an admin reads the owner-facing endpoint, which names whose records are being
reviewed.

**One viewer was added back, 2026-09-27: the mentor themself.** A mentor reads
their own profile and reviews in any state, which makes the profile and its
reviews the first responses here that vary by caller — so both send
`Vary: Authorization`, and `Cache-Control: private` whenever a token came with
the request. `/mentors` itself is unchanged and still identical for everyone.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from app.api.deps import MentorPageDep, MentorReviewsDep, PublicMentorDep
from app.api.schemas.common import Page
from app.api.schemas.mentors import MentorPage, MentorPublicRead, MentorSummaryRead
from app.api.schemas.reviews import MentorReviewRead

router = APIRouter(prefix="/api/v1/mentors", tags=["public"])


def _per_viewer(request: Request, response: Response) -> None:
    """Mark a response that the caller's token may have changed.

    **`Vary` always**, because a cache holding the anonymous copy must not hand
    it to the owner, whose token changes the answer. **`private` whenever a token
    came**, so no shared cache keeps a signed-in view at all — the rule agreed
    for every response that depends on who is asking.
    """
    response.headers["Vary"] = "Authorization"
    if "authorization" in request.headers:
        response.headers["Cache-Control"] = "private"


PUBLIC_RESPONSES: dict[int | str, dict[str, str]] = {
    status.HTTP_404_NOT_FOUND: {
        "description": (
            "No such mentor. Covers a handle that is nobody, a user who is not a "
            "mentor, an unapproved or unlisted one, and a soft-deleted profile or "
            "account — indistinguishable on purpose, because telling them apart "
            "says which mentors exist and what state they are in."
        )
    },
}


@router.get(
    "",
    response_model=MentorPage,
    summary="Find a mentor",
    description=(
        "Every mentor a mentee could actually book, newest first.\n\n"
        "**Public.** No token — this is the page somebody lands on before they "
        "have an account.\n\n"
        "**Bookable, not available.** A mentor appears while they are approved, "
        "listed, and set up: at least one active offering with a duration, and "
        "at least one weekly availability window. It says nothing about *when* "
        "they are free — a mentor booked solid for a month still appears, "
        "because they exist and they take this kind of work. Ask "
        "`/users/{id}/availability/slots` for the calendar.\n\n"
        "A mentor who has not finished setting up does not appear here at all, "
        "though their profile still resolves by direct link.\n\n"
        "**`next_available_at`** is when the mentor is next bookable, stored and "
        "refreshed every few minutes (ADR 0029) — a display hint; booking always "
        "reads live slots. It is non-null only when `next_available_state` is "
        "`open`. `none` means nothing free in the booking horizon; `refreshing` "
        "means not recomputed since a booking or hours change — unknown, not "
        "empty.\n\n"
        "**`total`** is how many mentors the request lists across every page, "
        "sent on the first page only and `null` after it.\n\n"
        "**`offering` filters by service offering**, repeatable, any of: "
        "`?offering=a&offering=b` lists mentors who give either, and it narrows "
        "a `q` search to those mentors. Slugs come from "
        "`/api/v1/catalog/service-offerings`; an unknown or retired one is a "
        "`422`. School, degree and country filters are still to come.\n\n"
        "`offerings` is what kind of help each mentor gives — the platform "
        "taxonomy, and what matching runs on. What can be *booked* is on the "
        "profile."
    ),
    responses={
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "The `cursor` was not one this endpoint issued."
        }
    },
)
async def find_mentors(page: MentorPageDep) -> MentorPage:
    # The token is minted in the dependency, which is the only place that knows
    # which mode ran. Deriving it again here from `q` would be one rule in two
    # places, and the copy that drifted would mint the wrong kind.
    rows, _, next_cursor, total = page
    return MentorPage(
        data=[MentorSummaryRead.from_row(row) for row in rows],
        next_cursor=next_cursor,
        total=total,
    )


@router.get(
    "/{handle}",
    response_model=MentorPublicRead,
    summary="A mentor's public profile",
    description=(
        "Everything the public may read about one mentor, with what they offer "
        "and what can be booked.\n\n"
        "**Public.** No token is required — a mentee compares mentors before "
        "signing up. A mentor appears only while they are both approved and "
        "listed, so pausing removes them from here as well as from search.\n\n"
        "**Except to themselves.** With a bearer token, a mentor reads their own "
        "profile in any state — pending, declined or unlisted — and the response "
        "adds `approval_status` and `listing_status`, which nobody else ever "
        "receives. A hidden profile's `session_types` is empty and its "
        "`next_available_state` is `none`: strangers can book none of it.\n\n"
        "**`handle` is an id or a slug.** The slug is the legacy public profile "
        "handle, carried so existing profile links keep working; it is nullable, "
        "and a mentor without one is reachable by id.\n\n"
        "`session_types` is inlined because a profile page needs it and a second "
        "round trip for a handful of rows is waste. It is read by the same "
        "function that serves `/users/{id}/session-types`, so the two can never "
        "disagree — pass a `session_types[].id` to "
        "`/users/{id}/availability/slots` to see when that offering is free.\n\n"
        "`offerings` is a different thing: the closed platform taxonomy of *what "
        "kind of help* this mentor gives, which is what matching runs on. A "
        "session type is the bookable product."
    ),
    responses=PUBLIC_RESPONSES,
)
async def read_public_mentor(
    mentor: PublicMentorDep, request: Request, response: Response
) -> MentorPublicRead:
    _per_viewer(request, response)
    return MentorPublicRead.from_row(
        mentor["row"],
        mentor["offerings"],
        mentor["session_types"],
        mentor["education"],
        mentor["scholarships"],
        mentor["languages"],
        mentor["stats"],
        mentor["reviews"],
    )


@router.get(
    "/{handle}/reviews",
    response_model=Page[MentorReviewRead],
    summary="What mentees said about this mentor",
    description=(
        "One page of published reviews, newest first.\n\n"
        "**Public**, like the profile it belongs to, and scoped the same way: a "
        "mentor who is paused or unapproved answers `404` here exactly as they "
        "do there.\n\n"
        "**Attribution is a first name and an initial.** The surname is never "
        "sent.\n\n"
        "`session_value` on a row is that review's own answer to *how valuable "
        "was this session*, `1..5` — the badge beside it. The mentor's overall "
        "figures are on the profile, not repeated per row.\n\n"
        "Withdrawn reviews are absent, which is the whole point of withdrawing "
        "one.\n\n"
        "A mentor reads their own reviews with a bearer token whatever state "
        "their profile is in, as on the profile."
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "No such mentor, or they are not publicly visible."
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "The `cursor` was not one this endpoint issued."
        },
    },
)
async def read_mentor_reviews(
    page: MentorReviewsDep, request: Request, response: Response
) -> Page[MentorReviewRead]:
    _per_viewer(request, response)
    rows, next_cursor = page
    return Page(data=[MentorReviewRead.from_row(row) for row in rows], next_cursor=next_cursor)
