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
reviewed. Dropping the other two removes the need for an optional-token
dependency, which this codebase has no shape for and which would make every
response vary by caller.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import PublicMentorDep
from app.api.schemas.mentors import MentorPublicRead

router = APIRouter(prefix="/api/v1/mentors", tags=["public"])

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
    "/{handle}",
    response_model=MentorPublicRead,
    summary="A mentor's public profile",
    description=(
        "Everything the public may read about one mentor, with what they offer "
        "and what can be booked.\n\n"
        "**Public.** No token is required — a mentee compares mentors before "
        "signing up. A mentor appears only while they are both approved and "
        "listed, so pausing removes them from here as well as from search.\n\n"
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
async def read_public_mentor(mentor: PublicMentorDep) -> MentorPublicRead:
    return MentorPublicRead.from_row(mentor["row"], mentor["offerings"], mentor["session_types"])
