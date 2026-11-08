"""An admin's preview sessions (decision 309).

**Three gates, in this order**, every one a plain `404` as every admin route
answers: the feature switched off (`PREVIEW_AS_USER_ENABLED`), then a caller
without the PREVIEW permission (`require_admin`, read from the one set in
`domain.admin_permissions`), then the preview not being the caller's in this
sign-in. The first runs as a router dependency, before the token is read, so
an off switch hides the routes from everyone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends

from app.api.deps.core import (
    ClaimsDep,
    CurrentUserDep,
    SessionDep,
    preview_switched_on,
    require_admin,
)
from app.api.schemas.preview import (
    PreviewExit,
    PreviewPageView,
    PreviewReveal,
    PreviewSessionRead,
    PreviewStart,
    PreviewSwitch,
)
from app.core.errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PreviewUserNotFoundError,
)
from app.domain.admin_permissions import PREVIEW, REVEAL, holds_any
from app.infra.db import preview_store
from app.infra.db.preview_store import Owner

NOT_LIVE = "this preview has ended"


PreviewOnDep = Annotated[bool, Depends(preview_switched_on)]


def require_preview_on(on: PreviewOnDep) -> None:
    """Every preview endpoint is `404` while the feature is off."""
    if not on:
        raise NotFoundError("no such endpoint")


def can_preview_as_user(user: CurrentUserDep, on: PreviewOnDep) -> bool:
    """`/me`'s flag: the caller holds the PREVIEW permission and previews are
    on. The same set and switch as the routes' gates, so the two agree."""
    return on and holds_any(user["admin_roles"], PREVIEW)


CanPreviewDep = Annotated[bool, Depends(can_preview_as_user)]

_PREVIEW_GATE = require_admin(*PREVIEW)


@dataclass(frozen=True, slots=True)
class PreviewCaller:
    """An admin who may preview, in one sign-in, and whether they may reveal."""

    owner: Owner
    can_reveal: bool


def preview_caller(
    admin_id: Annotated[UUID, Depends(_PREVIEW_GATE)], user: CurrentUserDep, claims: ClaimsDep
) -> PreviewCaller:
    """The caller as a preview's owner. A token naming no sign-in can own no
    preview, so it is refused as any unusable token is (`401`)."""
    if claims.session_id is None:
        raise AuthenticationError("token carries no session")
    return PreviewCaller(
        owner=Owner(admin_id=admin_id, auth_session_id=claims.session_id),
        can_reveal=holds_any(user["admin_roles"], REVEAL),
    )


PreviewCallerDep = Annotated[PreviewCaller, Depends(preview_caller)]


def reveal_caller(caller: PreviewCallerDep) -> PreviewCaller:
    """A preview caller who may also reveal: `404` otherwise, as any admin
    route answers, and before the body is read. The same `holds_any` over
    REVEAL as `can_reveal`, so the gate and the flag cannot disagree."""
    if not caller.can_reveal:
        raise NotFoundError("no such endpoint")
    return caller


RevealCallerDep = Annotated[PreviewCaller, Depends(reveal_caller)]


async def _target(session: SessionDep, caller: PreviewCaller, **match: Any) -> UUID:
    found = await preview_store.find_previewable(session, caller.owner.admin_id, **match)
    if found is None:
        raise PreviewUserNotFoundError("no user you may preview matches")
    return found


async def _read(session: SessionDep, caller: PreviewCaller, preview_id: UUID) -> Any:
    """The caller's preview, a lapsed one ended on the way (committed, so the
    end stands whatever the request does next); `404` when not theirs."""
    row = await preview_store.load(session, caller.owner, preview_id)
    await session.commit()
    if row is None:
        raise NotFoundError("no such preview")
    return row


async def started_preview(
    payload: PreviewStart, caller: PreviewCallerDep, session: SessionDep
) -> PreviewSessionRead:
    """Start previewing a user, ending the caller's live preview in this
    sign-in first."""
    target = await _target(session, caller, user_id=payload.user_id, email=payload.email)
    preview_id = await preview_store.start(session, caller.owner, target)
    await session.commit()
    return PreviewSessionRead.of(
        await _read(session, caller, preview_id), can_reveal=caller.can_reveal
    )


async def switched_preview(
    preview_id: UUID, payload: PreviewSwitch, caller: PreviewCallerDep, session: SessionDep
) -> PreviewSessionRead:
    """Preview another user in the same live preview. `409` once it has ended,
    whoever is asked for."""
    if (await _read(session, caller, preview_id)).ended_at is not None:
        raise ConflictError(NOT_LIVE)
    target = await _target(session, caller, user_id=payload.user_id)
    if not await preview_store.switch(session, caller.owner, preview_id, target):
        raise ConflictError(NOT_LIVE)
    await session.commit()
    return PreviewSessionRead.of(
        await _read(session, caller, preview_id), can_reveal=caller.can_reveal
    )


async def read_preview(
    preview_id: UUID, caller: PreviewCallerDep, session: SessionDep
) -> PreviewSessionRead:
    """One of the caller's previews, live or ended."""
    return PreviewSessionRead.of(
        await _read(session, caller, preview_id), can_reveal=caller.can_reveal
    )


async def ended_preview(
    preview_id: UUID, payload: PreviewExit, caller: PreviewCallerDep, session: SessionDep
) -> None:
    """End the caller's preview; ending an ended one changes nothing."""
    await _read(session, caller, preview_id)
    await preview_store.end(session, caller.owner, preview_id, payload.reason)
    await session.commit()


async def logged_page_view(
    preview_id: UUID, payload: PreviewPageView, caller: PreviewCallerDep, session: SessionDep
) -> None:
    """Log a page opened in the caller's live preview. `409` once it has ended."""
    await _read(session, caller, preview_id)
    if not await preview_store.log_page_view(session, caller.owner, preview_id, payload.path):
        raise ConflictError(NOT_LIVE)
    await session.commit()


async def revealed_preview(
    preview_id: UUID, payload: PreviewReveal, caller: RevealCallerDep, session: SessionDep
) -> PreviewSessionRead:
    """Reveal the previewed user's messages, for this user only (decision 314).
    Revealing a revealed user changes nothing. `409` once the preview has ended."""
    await _read(session, caller, preview_id)
    if not await preview_store.reveal(session, caller.owner, preview_id, payload.reason):
        raise ConflictError(NOT_LIVE)
    await session.commit()
    return PreviewSessionRead.of(
        await _read(session, caller, preview_id), can_reveal=caller.can_reveal
    )


StartedPreviewDep = Annotated[PreviewSessionRead, Depends(started_preview)]
SwitchedPreviewDep = Annotated[PreviewSessionRead, Depends(switched_preview)]
PreviewDep = Annotated[PreviewSessionRead, Depends(read_preview)]
EndedPreviewDep = Annotated[None, Depends(ended_preview)]
LoggedPageViewDep = Annotated[None, Depends(logged_page_view)]
RevealedPreviewDep = Annotated[PreviewSessionRead, Depends(revealed_preview)]
