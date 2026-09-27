"""The avatar focal-point backfill's reads and writes.

New uploads get a focal point as they are stored (`asset_store.store_image`).
This is for avatars stored before that existed: find the ones never looked at,
and record what detection finds.

**Never overwrites a mentor's own choice, or a newer photo.** The write is
conditional on the row still holding the same `avatar_url` and still having no
focus source, so a mentor who chose a crop — or uploaded a new photo — while the
backfill ran keeps what they did.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.avatar_focus import DETECTED
from app.infra.db.models.user import User, UserProfile
from app.infra.db.predicates import LIVE
from app.infra.images.faces import FaceDetectionError, avatar_focus
from app.infra.storage.supabase import StorageError, SupabaseStorage

__all__ = [
    "USER_STATEMENTS",
    "backfill_avatar_focus",
    "focus_columns",
    "record_focus",
    "unprocessed_avatars",
]

logger = logging.getLogger(__name__)

#: Live users with an avatar nobody has looked at yet.
UNPROCESSED = (
    select(UserProfile.user_id, UserProfile.avatar_url)
    .join(User, User.id == UserProfile.user_id)
    .where(
        LIVE,
        UserProfile.avatar_url.is_not(None),
        UserProfile.avatar_focus_source.is_(None),
    )
    .order_by(UserProfile.user_id)
)

#: Statements reading an existing `users` row, for the `LIVE` parity test.
USER_STATEMENTS = (UNPROCESSED,)


def focus_columns(focus: tuple[float, float] | None, *, looked: bool) -> dict[str, object]:
    """The profile columns for one detection outcome — the only place they are built.

    `looked=False` (detection failed) leaves everything empty, so the backfill
    tries again. `looked=True` with no point records `detected`: looked, no face.
    """
    if not looked:
        return {"avatar_focus_x": None, "avatar_focus_y": None, "avatar_focus_source": None}
    x, y = focus if focus is not None else (None, None)
    return {"avatar_focus_x": x, "avatar_focus_y": y, "avatar_focus_source": DETECTED}


async def unprocessed_avatars(session: AsyncSession) -> list[tuple[UUID, str]]:
    """`(user_id, avatar_url)` for every live avatar without a focus decision."""
    rows = await session.execute(UNPROCESSED)
    return [(user_id, str(url)) for user_id, url in rows]


async def record_focus(
    session: AsyncSession, user_id: UUID, url: str, focus: tuple[float, float] | None
) -> bool:
    """Record a detection for the photo at `url`, unless something changed since.

    `focus` of `None` still records `detected`: looked, found no face, so the
    next run does not download it again. Returns whether a row was written.
    Does not commit.
    """
    result = await session.execute(
        update(UserProfile)
        .where(
            UserProfile.user_id == user_id,
            UserProfile.avatar_url == url,
            UserProfile.avatar_focus_source.is_(None),
        )
        .values(**focus_columns(focus, looked=True))
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def backfill_avatar_focus(session: AsyncSession, storage: SupabaseStorage) -> dict[str, int]:
    """Find the face in every unprocessed avatar and record it. Commits per avatar.

    **Carries on past a bad one.** An avatar URL this bucket does not own (a
    legacy link, never re-hosted) is skipped and left unprocessed, so fixing it
    later lets a re-run pick it up. One that cannot be downloaded is logged and
    skipped the same way. Neither is recorded as "no face", which would be a
    claim about a picture nobody looked at.
    """
    counts = {"focused": 0, "no_face": 0, "skipped": 0, "unreadable": 0, "changed": 0}
    for user_id, url in await unprocessed_avatars(session):
        path = storage.path_of(url)
        if path is None:
            counts["skipped"] += 1
            continue
        try:
            payload = await asyncio.to_thread(storage.download, path)
            focus = await asyncio.to_thread(avatar_focus, payload)
        except StorageError, httpx.HTTPError, FaceDetectionError:
            # A timeout, a missing object or an undecodable one: nobody looked
            # at the picture, so nothing is recorded and a re-run tries again.
            logger.warning("avatar could not be read for focus", extra={"user_id": str(user_id)})
            counts["unreadable"] += 1
            continue
        if await record_focus(session, user_id, url, focus):
            counts["focused" if focus is not None else "no_face"] += 1
        else:
            counts["changed"] += 1
        await session.commit()
    return counts
