"""An admin's preview of the app as one user, read only (decision 309).

Called **without** the `X-EF-Preview` header: these manage the preview, they
are not inside it. Every route is `404` while `PREVIEW_AS_USER_ENABLED` is off,
and to a caller without the PREVIEW permission, as every admin route is.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import (
    EndedPreviewDep,
    LoggedPageViewDep,
    PreviewDep,
    StartedPreviewDep,
    SwitchedPreviewDep,
    require_preview_on,
)
from app.api.routes.admin import ADMIN_RESPONSES
from app.api.schemas.preview import PreviewSessionRead
from app.domain.preview import IDLE_WORDS, MAX_LENGTH_WORDS, PAGE_PATH_MAX_LENGTH

PREFIX = "/api/v1/admin/preview-sessions"

router = APIRouter(prefix=PREFIX, tags=["admin"], dependencies=[Depends(require_preview_on)])

#: Who owns a preview, said once for every route that reads one.
_OWNED = (
    "A preview belongs to the admin who started it **in the sign-in they "
    "started it in** (the access token's `session_id`): another admin's, or "
    "this admin's from another sign-in, answers `404` like an unknown id."
)

_NOT_LIVE: dict[int | str, dict[str, str]] = {
    status.HTTP_409_CONFLICT: {
        "description": "The preview has ended (`ended_at` is set): start a new one."
    }
}

_NO_USER: dict[int | str, dict[str, str]] = {
    status.HTTP_404_NOT_FOUND: {
        "description": (
            ADMIN_RESPONSES[status.HTTP_404_NOT_FOUND]["description"]
            + "\n\nWith `type` `/problems/preview-user-not-found`: no user you may "
            "preview matches. Previewable is a live account holding no live admin "
            "grant that is not you, so an unknown, deleted or admin account, or "
            "yourself, all answer this."
        )
    }
}

_LAPSE = (
    f"A live preview whose time ran out ({IDLE_WORDS} with no request: `idle`; "
    f"{MAX_LENGTH_WORDS} since it started: `max_length`) is ended by any call that "
    "reads it, at the moment it lapsed, and logged."
)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=PreviewSessionRead,
    summary="Start previewing the app as a user",
    description=(
        "Starts a read-only preview of the app as one mentor or mentee, chosen "
        "by `user_id` or `email` (exactly one; an email matches ignoring case). "
        "**Ends the caller's live preview in this sign-in first** (with "
        "`end_reason` `exit`), so a sign-in has at most one. Logs `start`. "
        "`Location` names the new preview.\n\n"
        "Admins with the PREVIEW permission (every live grant today)."
    ),
    responses=ADMIN_RESPONSES | _NO_USER,
)
async def start_preview(preview: StartedPreviewDep, response: Response) -> PreviewSessionRead:
    response.headers["Location"] = f"{PREFIX}/{preview.id}"
    return preview


@router.patch(
    "/{preview_id}",
    response_model=PreviewSessionRead,
    summary="Preview another user",
    description=(
        "Switches a live preview to another user. The time with the user "
        "restarts and a reveal is cleared (it was for the user before); the "
        "preview's own start, and so its `expires_at`, does not move. Logs "
        f"`switch`.\n\n{_OWNED} {_LAPSE}"
    ),
    responses=ADMIN_RESPONSES | _NO_USER | _NOT_LIVE,
)
async def switch_preview(preview: SwitchedPreviewDep) -> PreviewSessionRead:
    return preview


@router.get(
    "/{preview_id}",
    response_model=PreviewSessionRead,
    summary="One of my previews",
    description=(
        f"A preview, live or ended (`ended_at` and `end_reason` set together). {_LAPSE}\n\n{_OWNED}"
    ),
    responses=ADMIN_RESPONSES,
)
async def read_preview(preview: PreviewDep) -> PreviewSessionRead:
    return preview


@router.post(
    "/{preview_id}/end",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="End a preview",
    description=(
        "Ends a preview with `reason` `exit` (the admin left it) or `sign_out` "
        "(the admin is signing out), and logs `end`. **Idempotent**: ending an "
        "ended preview answers `204` and changes nothing, its first reason "
        f"included.\n\n{_OWNED}"
    ),
    responses=ADMIN_RESPONSES,
)
async def end_preview(_: EndedPreviewDep) -> None:
    return None


@router.post(
    "/{preview_id}/page-views",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Log a page opened in a preview",
    description=(
        "Logs that the admin opened `path` in a live preview, and counts as "
        "activity for the idle limit. **The path only**: a query string or "
        "fragment is dropped before anything is stored; it must start with `/` "
        f"and be at most {PAGE_PATH_MAX_LENGTH} characters (`422` otherwise).\n\n"
        f"{_OWNED} {_LAPSE}"
    ),
    responses=ADMIN_RESPONSES | _NOT_LIVE,
)
async def log_page_view(_: LoggedPageViewDep) -> None:
    return None
