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
    CreatedSessionTypeWindowDep,
    DeletedOwnSessionTypeDep,
    DeletedSessionTypeWindowDep,
    OwnSessionTypesDep,
    RestoredOwnSessionTypeDep,
    SessionTypeWindowsDep,
    UpdatedOwnSessionTypeDep,
    UpdatedSessionTypeWindowDep,
)
from app.api.routes.sessions import REPLAYED_HEADER
from app.api.schemas.availability import AvailabilityRuleRead
from app.api.schemas.common import Page
from app.api.schemas.session_types import (
    DeletionScheduledRead,
    OwnSessionTypeRead,
    SessionTypeCreated,
)
from app.core.errors import NotFoundError

router = APIRouter(prefix="/api/v1/me", tags=["session-types"])

OWNER_RESPONSES: dict[int | str, dict[str, str]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "The bearer token is absent, malformed, expired or wrongly signed."
    },
    status.HTTP_404_NOT_FOUND: {
        "description": (
            "The token is valid but has no live account and nothing to create one "
            "from: it carries no email, or its account was deleted. A first "
            "sign-in with an email creates the account (settled decision #178)."
        )
    },
    status.HTTP_409_CONFLICT: {
        "description": (
            "`/problems/account-exists`: a first sign-in whose email belongs to "
            "an account this sign-in is not linked to. Never linked or merged "
            "automatically; the person should contact support."
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
        "Adds to the public shape `is_active`, each offering's own approval, "
        "window and break settings, and `duration_inherited` / "
        "`min_notice_inherited`, which say whether its length and notice are "
        "its own or follow your defaults. "
        "`service_offering`, `application_stage` and `custom_stage_label` were "
        "owner-only while they were free text with no vocabulary — publishing "
        "them would have fixed a public contract to an undesigned shape — and "
        "both are public now that each has one.\n\n"
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
    response_model=SessionTypeCreated,
    summary="Create a session type",
    description=(
        "The offering and its booking settings are created **together**, in one "
        "transaction.\n\n"
        "**`duration_minutes` and `min_notice_minutes` may be left out, or sent "
        "as `null`, to follow your defaults** (`default_duration_minutes` and "
        "`default_min_notice_minutes` on your mentor profile), then the "
        "platform's: 60 minutes and 24 hours. The reads return the resolved "
        "value either way.\n\n"
        "**Notice is 24 to 72 hours.** The floor is a platform rule — no "
        "same-day booking — and it is the reason a value below `1440` is refused "
        "here rather than stored.\n\n"
        "**`meeting_venue` is not writable yet.** A new offering is held "
        "wherever your default conferencing option says, and the read models "
        "resolve it. Choosing a venue per offering needs a surface for managing "
        "those options, which does not exist yet.\n\n"
        "**A new offering is active.** There is no draft state, so there is "
        "nothing to publish — `is_active` is writable on `PATCH`, where "
        "switching one off is the point.\n\n"
        "A caller with no mentor profile gets `404`: a session type belongs to a "
        "mentor, and there is no true empty answer to a write.\n\n"
        "**`questions`** (at most five) are created with the offering in one "
        "transaction; an invalid question refuses the whole request. The response "
        "is `{id, question_ids}`.\n\n"
        "**`Idempotency-Key` is optional.** Sent, a retry with the same key and "
        "body replays the first answer (with `Idempotent-Replayed: true`) instead "
        "of creating a second offering; the same key with a different body is a "
        "`422`. Absent, nothing changes."
    ),
    responses=WRITE_RESPONSES | NAME_CONFLICT,
)
async def create_own_session_type(
    created: CreatedOwnSessionTypeDep, response: Response
) -> SessionTypeCreated:
    body, status_code, replayed = created
    response.status_code = status_code
    if replayed:
        response.headers[REPLAYED_HEADER] = "true"
    response.headers["Location"] = "/api/v1/me/session-types"
    # Validated, so a body replayed out of JSONB is the same typed answer.
    return SessionTypeCreated.model_validate(body)


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
        "**With sessions still booked on it, it is scheduled rather than refused** "
        "(`202`): hidden at once, un-featured, and deleted automatically once "
        "the last of them is over — they go ahead. A session awaiting your "
        "decision, or already agreed, holds it; cancelled and completed ones do "
        "not. `pending_deletion` on your list says when, and "
        "`POST .../restore` cancels it. Deleting it again answers the same "
        "schedule.\n\n"
        "**Switching off is the reversible alternative** and is usually what is "
        "wanted: `PATCH` with `is_active: false` makes an offering invisible and "
        "unbookable while leaving it to switch back on. A deletion that has "
        "happened is not reversible through this API.\n\n"
        "The name becomes free immediately — a deleted offering does not reserve "
        "it.\n\n"
        "An offering that is not yours, or is already deleted, gets `404`."
    ),
    responses=WRITE_RESPONSES
    | {
        status.HTTP_202_ACCEPTED: {
            "model": DeletionScheduledRead,
            "description": (
                "Sessions are still booked on this offering, so its deletion is "
                "scheduled for after the last of them."
            ),
        }
    },
)
async def remove_own_session_type(removed: DeletedOwnSessionTypeDep) -> Response:
    if removed is False:
        raise NotFoundError("no such session type")
    if removed is True:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    body = DeletionScheduledRead(
        deletes_after=removed.deletes_after, booked_count=removed.booked_count
    )
    return Response(
        content=body.model_dump_json(),
        status_code=status.HTTP_202_ACCEPTED,
        media_type="application/json",
    )


@router.post(
    "/session-types/{session_type_id}/restore",
    response_model=OwnSessionTypeRead,
    summary="Cancel a scheduled deletion",
    description=(
        "Cancels the deletion scheduled by a `DELETE` on an offering with booked "
        "sessions. The offering **stays hidden**; show it again with `PATCH "
        '{"is_active": true}`. Answers the offering as your list shows it. A '
        "no-op on an offering with nothing scheduled.\n\n"
        "An offering that is not yours, or is deleted, gets `404`."
    ),
    responses=WRITE_RESPONSES,
)
async def restore_own_session_type(restored: RestoredOwnSessionTypeDep) -> OwnSessionTypeRead:
    return OwnSessionTypeRead.from_row(restored)


WINDOW_DESCRIPTION = (
    "An offering's **own** weekly hours. **An offering with windows is bookable in "
    "them and nowhere else**: your general availability no longer applies to it, "
    "while dates you blocked still do. An offering with none uses your general "
    "availability. A window has the same shape as a rule in "
    "`/users/{id}/availability/rules`: `day_of_week` (0 = Sunday), wall-clock "
    "`start_time`/`end_time` and an IANA `timezone`; a window crossing midnight is "
    "two, one per weekday. Two windows on **one** offering may not overlap on a "
    "weekday (409); different offerings may share hours."
)


@router.get(
    "/session-types/{session_type_id}/windows",
    response_model=Page[AvailabilityRuleRead],
    summary="An offering's own weekly hours",
    description=WINDOW_DESCRIPTION,
    responses=OWNER_RESPONSES,
)
async def list_session_type_windows(windows: SessionTypeWindowsDep) -> Page[AvailabilityRuleRead]:
    return Page(data=[AvailabilityRuleRead.from_row(row) for row in windows], next_cursor=None)


@router.post(
    "/session-types/{session_type_id}/windows",
    status_code=status.HTTP_201_CREATED,
    summary="Add a weekly window to an offering",
    description=WINDOW_DESCRIPTION,
    responses=OWNER_RESPONSES,
)
async def add_session_type_window(
    created: CreatedSessionTypeWindowDep, session_type_id: str, response: Response
) -> dict[str, str]:
    response.headers["Location"] = f"/api/v1/me/session-types/{session_type_id}/windows/{created}"
    return {"id": str(created)}


@router.patch(
    "/session-types/{session_type_id}/windows/{window_id}",
    summary="Change one of an offering's windows",
    description=(
        "Only the fields sent change. Moving onto another window of this offering is a 409."
    ),
    responses=OWNER_RESPONSES,
)
async def edit_session_type_window(changed: UpdatedSessionTypeWindowDep) -> dict[str, bool]:
    if not changed:
        raise NotFoundError("no such window")
    return {"updated": True}


@router.delete(
    "/session-types/{session_type_id}/windows/{window_id}",
    summary="Remove one of an offering's windows",
    description=(
        "Soft delete: the window stops being offered and stops blocking its hours. "
        "Removing an offering's last window returns it to your general availability."
    ),
    responses=OWNER_RESPONSES,
)
async def remove_session_type_window(removed: DeletedSessionTypeWindowDep) -> dict[str, bool]:
    if not removed:
        raise NotFoundError("no such window")
    return {"deleted": True}
