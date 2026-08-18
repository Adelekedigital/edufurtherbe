"""A mentor's own session types — the management surface, not the shop window.

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
sub-resources under it. The write surface has since landed here and the tag has
not moved: a `session-types` tag may still earn its place once `DELETE` joins
them, and a tag is documentation grouping rather than a wire contract, so
regrouping breaks nothing whenever that happens.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.api.deps import (
    CreatedOwnSessionTypeDep,
    DeletedOwnSessionTypeDep,
    OwnSessionTypesDep,
    UpdatedOwnSessionTypeDep,
)
from app.api.schemas.common import Page
from app.api.schemas.session_types import OwnSessionTypeRead
from app.core.errors import NotFoundError

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


WRITE_RESPONSES: dict[int | str, dict[str, str]] = OWNER_RESPONSES | {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "The body failed validation."},
}

NAME_CONFLICT: dict[int | str, dict[str, str]] = {
    status.HTTP_409_CONFLICT: {
        "description": (
            "You already have a live offering with this name. Names must be "
            "distinguishable because a mentee choosing between two identical "
            "ones cannot tell them apart. A **deleted** offering does not "
            "reserve its name."
        )
    }
}


@router.post(
    "/session-types",
    status_code=status.HTTP_201_CREATED,
    summary="Create a session type",
    description=(
        "The offering and its booking settings are created **together**, in one "
        "transaction. Duration and notice are not optional extras: slots are "
        "computed from them, so an offering without them could not be booked "
        "and there is no second request that would supply them later.\n\n"
        "**`min_notice_minutes` is 24 hours by default and may be raised to "
        "72.** The floor is a platform rule — no same-day booking — and it is "
        "the reason a value below `1440` is refused here rather than stored. "
        "Sending nothing takes the floor.\n\n"
        "**`meeting_venue` is not writable yet.** A new offering is held "
        "wherever your default conferencing option says, and the read models "
        "resolve it. Choosing a venue per offering needs a surface for managing "
        "those options, which does not exist yet.\n\n"
        "**A new offering is active.** There is no draft state, so there is "
        "nothing to publish — `is_active` is writable on `PATCH`, where "
        "switching one off is the point.\n\n"
        "A caller with no mentor profile gets `404`: a session type belongs to a "
        "mentor, and there is no true empty answer to a write."
    ),
    responses=WRITE_RESPONSES | NAME_CONFLICT,
)
async def create_own_session_type(
    session_type_id: CreatedOwnSessionTypeDep, response: Response
) -> dict[str, str]:
    response.headers["Location"] = "/api/v1/me/session-types"
    return {"id": str(session_type_id)}


@router.patch(
    "/session-types/{session_type_id}",
    summary="Change one of your session types",
    description=(
        "Every field is optional and **absent is not null** — a field you do "
        "not send is left alone rather than cleared.\n\n"
        "**`is_active` switches an offering off and on, and nothing refuses "
        "it.** Deactivating hides it from new bookings and leaves existing ones "
        "untouched; `false` covers off, closed and hidden alike, so a "
        "deactivated offering is invisible in search *and* unbookable by direct "
        "link.\n\n"
        "**A switched-off offering is still editable**, which is what switching "
        "it back on requires — this endpoint scopes on ownership and deletion, "
        "never on whether the offering is currently on offer.\n\n"
        "An offering that is not yours, or is deleted, gets `404`. Not "
        "`403`: that would confirm the id exists."
    ),
    responses=WRITE_RESPONSES | NAME_CONFLICT,
)
async def edit_own_session_type(changed: UpdatedOwnSessionTypeDep) -> dict[str, bool]:
    if not changed:
        raise NotFoundError("no such session type")
    return {"updated": True}


@router.delete(
    "/session-types/{session_type_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete one of your session types",
    description=(
        "Removes the offering from your list and from everything a mentee can "
        "see or book. Past sessions keep pointing at it, so their history stays "
        "readable — the row survives, marked deleted.\n\n"
        "**Refused with `409` while sessions are still booked on it.** A session "
        "awaiting your decision, or already agreed, is somebody's plan; cancel "
        "them or let them finish first. Cancelled and completed sessions do not "
        "hold an offering open.\n\n"
        "**Switching off is the reversible alternative** and is usually what is "
        "wanted: `PATCH` with `is_active: false` makes an offering invisible and "
        "unbookable while leaving it to switch back on. Deletion is not "
        "reversible through this API.\n\n"
        "The name becomes free immediately — a deleted offering does not reserve "
        "it.\n\n"
        "An offering that is not yours, or is already deleted, gets `404`."
    ),
    responses=WRITE_RESPONSES
    | {
        status.HTTP_409_CONFLICT: {
            "description": (
                "Sessions are still booked on this offering. **The only refusal "
                "this endpoint has**, which is why it carries no machine-readable "
                "reason: the primary-offering refusal it once had to be "
                "distinguished from no longer exists. A second reason would "
                "bring one back."
            )
        }
    },
)
async def remove_own_session_type(removed: DeletedOwnSessionTypeDep) -> None:
    if not removed:
        raise NotFoundError("no such session type")
