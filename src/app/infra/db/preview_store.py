"""An admin's preview sessions and the preview log (decision 309).

**Every statement is scoped to its owner in the statement**: the admin *and*
the sign-in (`Owner`), never fetched and checked after (non-negotiable 5). A
preview another admin, or another sign-in of the same admin, started is not
found, whatever its id.

**One clock**: a preview's times are the database's. A read carries `now()`
beside the row, the lapse rule (`domain.preview.lapse`) is judged on it, and
every write stamps `now()` in the same transaction. Nothing here commits.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, NamedTuple
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.emails import normalise_email
from app.domain.enums import PreviewEndReason, PreviewEventKind
from app.domain.preview import lapse
from app.infra.db.models.preview import PreviewEvent, PreviewSession
from app.infra.db.models.user import User
from app.infra.db.predicates import LIVE, holds_live_grant
from app.infra.db.user_locks import lock_users

__all__ = [
    "Owner",
    "end",
    "find_previewable",
    "load",
    "log_page_view",
    "start",
    "switch",
]


class Owner(NamedTuple):
    """Who a preview belongs to: an admin, in one sign-in (the access token's
    `session_id` claim)."""

    admin_id: UUID
    auth_session_id: UUID


_SESSION = PreviewSession.__table__.c


def _owned(owner: Owner, preview_id: UUID) -> list[Any]:
    """This preview, if it is the owner's: the scope of every statement here."""
    return [
        _SESSION.id == preview_id,
        _SESSION.admin_user_id == owner.admin_id,
        _SESSION.auth_session_id == owner.auth_session_id,
    ]


def previewable(caller: UUID) -> list[Any]:
    """**Who an admin may preview**: a live account that holds no live admin
    grant and is not the caller."""
    return [LIVE, User.id != caller, ~holds_live_grant(User.id)]


async def find_previewable(
    session: AsyncSession, caller: UUID, *, user_id: UUID | None = None, email: str | None = None
) -> UUID | None:
    """The previewable user with this id, or else with this address (stored
    lowercase, so matched normalised); ``None`` when there is none."""
    match = (
        User.id == user_id if user_id is not None else User.email == normalise_email(email or "")
    )
    found: UUID | None = await session.scalar(select(User.id).where(match, *previewable(caller)))
    return found


def read_statement(owner: Owner, preview_id: UUID) -> Any:
    """One owned preview with its user, as the session shows them
    (`AdminUserRead`'s columns), and the database's `now()`. By primary key."""
    return (
        select(
            *_SESSION,
            User.id.label("user_id"),
            User.email,
            User.first_name,
            User.last_name,
            User.primary_role.label("role"),
            func.now().label("now"),
        )
        .join(User, User.id == _SESSION.target_user_id)
        .where(*_owned(owner, preview_id))
    )


async def _read(session: AsyncSession, owner: Owner, preview_id: UUID) -> Any | None:
    return (await session.execute(read_statement(owner, preview_id))).first()


async def _log(session: AsyncSession, row: Any, kind: PreviewEventKind, **detail: object) -> None:
    """One row of the preview log, naming the preview's admin and its user now."""
    await session.execute(
        insert(PreviewEvent).values(
            session_id=row.id,
            admin_user_id=row.admin_user_id,
            target_user_id=row.target_user_id,
            kind=kind,
            **detail,
        )
    )


_LOGGED = (_SESSION.id, _SESSION.admin_user_id, _SESSION.target_user_id)


async def _write(
    session: AsyncSession, owner: Owner, preview_id: UUID, **values: object
) -> Any | None:
    """Change an owned preview **while it is live**; ``None`` when it is not
    (ended, or ended by a racing request first, so the race logs once)."""
    result = await session.execute(
        update(PreviewSession)
        .where(*_owned(owner, preview_id), _SESSION.ended_at.is_(None))
        .values(**values)
        .returning(*_LOGGED)
    )
    return result.first()


async def end(
    session: AsyncSession,
    owner: Owner,
    preview_id: UUID,
    reason: PreviewEndReason,
    at: datetime | None = None,
) -> None:
    """End an owned live preview with ``reason`` (now, unless ``at`` says when)
    and log `end`; nothing when it has already ended, so ending is idempotent."""
    row = await _write(
        session,
        owner,
        preview_id,
        ended_at=func.now() if at is None else at,
        end_reason=reason,
    )
    if row is not None:
        await _log(session, row, PreviewEventKind.END, end_reason=reason)


async def load(session: AsyncSession, owner: Owner, preview_id: UUID) -> Any | None:
    """One owned preview, ended or live; ``None`` when it is not the owner's.

    **A live preview that has lapsed is ended here** (`idle` or `max_length`,
    at the moment it lapsed), by the one rule every reader applies, so no read
    shows a preview as live after its time.
    """
    row = await _read(session, owner, preview_id)
    if row is None or row.ended_at is not None:
        return row
    ran_out = lapse(started_at=row.started_at, last_active_at=row.last_active_at, now=row.now)
    if ran_out is None:
        return row
    await end(session, owner, preview_id, ran_out.reason, ran_out.at)
    return await _read(session, owner, preview_id)


async def start(session: AsyncSession, owner: Owner, target: UUID) -> UUID:
    """Start previewing ``target``, ending the owner's live preview in this
    sign-in first (`exit`, logged), and log `start`.

    **The admin's row is locked first** (`lock_users`), so two starts in one
    sign-in queue rather than both inserting and one failing on
    `uq_preview_sessions_live`.
    """
    await lock_users(session, [owner.admin_id])
    live = await session.scalars(
        select(_SESSION.id).where(
            _SESSION.admin_user_id == owner.admin_id,
            _SESSION.auth_session_id == owner.auth_session_id,
            _SESSION.ended_at.is_(None),
        )
    )
    for replaced in live.all():
        await end(session, owner, replaced, PreviewEndReason.EXIT)
    created = (
        await session.execute(
            insert(PreviewSession)
            .values(
                admin_user_id=owner.admin_id,
                auth_session_id=owner.auth_session_id,
                target_user_id=target,
            )
            .returning(*_LOGGED)
        )
    ).one()
    await _log(session, created, PreviewEventKind.START)
    preview_id: UUID = created.id
    return preview_id


async def switch(session: AsyncSession, owner: Owner, preview_id: UUID, target: UUID) -> bool:
    """Preview ``target`` instead, in the same live preview: its time with the
    user restarts, a reveal is cleared (it was for the user before), and
    `switch` is logged. ``False`` when the preview is not live."""
    row = await _write(
        session,
        owner,
        preview_id,
        target_user_id=target,
        target_since=func.now(),
        last_active_at=func.now(),
        revealed_at=None,
    )
    if row is None:
        return False
    await _log(session, row, PreviewEventKind.SWITCH)
    return True


async def log_page_view(session: AsyncSession, owner: Owner, preview_id: UUID, path: str) -> bool:
    """Log a page the admin opened in a live preview, which is activity: it
    moves `last_active_at`. ``False`` when the preview is not live."""
    row = await _write(session, owner, preview_id, last_active_at=func.now())
    if row is None:
        return False
    await _log(session, row, PreviewEventKind.PAGE_VIEW, path=path)
    return True
