"""A mentor's own session types — the management list, not the shop window.

**Its own module, and a second router on `/api/v1/me`.** `users.py` is
`prefix="/api/v1"` and already owns `/me`, so there is no path collision — but
two routers serving `/api/v1/me*` is a deliberate call rather than an accident.
It follows `routes/slots.py` and `routes/session_types.py`, which were split out
so that *which endpoints take a token* is visible in the file list rather than in
one decorator among ten. Here the split carries more: this module and
`session_types.py` serve the same rows to different audiences, and putting them
in one file is how a reader ends up believing there is one endpoint.

**`tags=["users"]` rather than a new tag.** Settled decision #64 gives the
`public` tag to non-catalogue public endpoints and everything else "its domain
name"; the `users` tag is described as "a user's own record and attributes", which
is exactly what this is, and `user_attributes.py` already groups the caller's own
sub-resources under it. When PRs 9 and 10 add the write surface, a `session-types`
tag may earn its place — a tag is documentation grouping, not a wire contract, so
regrouping later breaks nothing.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import OwnSessionTypesDep
from app.api.schemas.common import Page
from app.api.schemas.session_types import OwnSessionTypeRead

router = APIRouter(prefix="/api/v1/me", tags=["users"])

OWNER_RESPONSES: dict[int | str, dict[str, str]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "The bearer token is absent, malformed, expired or wrongly signed."
    },
    status.HTTP_404_NOT_FOUND: {
        "description": (
            "The token is genuine but no account is linked to it. Every migrated "
            "user is in that state until provisioning runs, so during cutover "
            "this is the ordinary case rather than an attack."
        )
    },
}


@router.get(
    "/session-types",
    response_model=Page[OwnSessionTypeRead],
    summary="Your own session types, including switched-off ones",
    description=(
        "Everything you offer, in the shape a management screen needs — with "
        "`is_active` so a paused offering can be shown as paused rather than "
        "simply missing.\n\n"
        "**This is not the same answer as "
        "`GET /users/{user_id}/session-types`.** That endpoint is public and "
        "shows what a mentee may book: only *active* offerings, and only while "
        "you are approved **and** listed. Two things follow that make it unusable "
        "as a management list — an offering you have switched off is absent from "
        "it entirely, and while your profile is unlisted or awaiting review it "
        "answers `404` for you as well as for everybody else. This endpoint "
        "ignores both: your own listing state is never consulted, and switched-off "
        "offerings are returned and flagged.\n\n"
        "Deleted offerings are the exception and stay absent — `is_active` is "
        "reversible and deletion is not.\n\n"
        "Adds `is_active`, `category` and `application_stage` to the public "
        "shape. The last two are free text with no vocabulary, null on every "
        "migrated row, and withheld publicly precisely because they are "
        "undesigned; you see them because they are yours.\n\n"
        "**A caller who is not a mentor gets `200` with an empty page**, not a "
        "refusal. A session type belongs to a mentor profile, so somebody without "
        'one cannot own any — and "you have none" is a true statement, where a '
        "`403` would be an answer to a question they did not ask.\n\n"
        "Ordered by name, which is unique among your undeleted offerings, so the "
        "order is total rather than merely usually-stable.\n\n"
        "`next_cursor` is always `null`. A mentor holds a handful of offerings, "
        "so the answer is returned whole; the envelope is here because ADR 0016 "
        "puts it on every list."
    ),
    responses=OWNER_RESPONSES,
)
async def read_own_session_types(session_types: OwnSessionTypesDep) -> Page[OwnSessionTypeRead]:
    return Page(data=[OwnSessionTypeRead.from_row(row) for row in session_types], next_cursor=None)
