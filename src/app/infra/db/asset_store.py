"""The database side of asset re-hosting.

Same shape and the same reason as ``provisioning_store.py``: ``scripts/`` is
outside ``mypy``, ``bandit`` and the coverage floor, so SQL written there is
checked by ruff alone (tier-2 row 44).

``LIVE`` is imported rather than re-typed. This module is the second consumer,
which is why the predicate moved to ``infra/db/predicates.py`` in this change.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import bindparam, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.domain.assets import AssetKind, object_path
from app.domain.images import process
from app.infra.db.avatar_focus_store import focus_columns
from app.infra.db.models.user import User
from app.infra.db.models.user import UserProfile as Profile
from app.infra.db.predicates import LIVE
from app.infra.db.triggers import timestamps_from_source
from app.infra.images.faces import FaceDetectionError, avatar_focus
from app.infra.storage.supabase import SupabaseStorage

logger = logging.getLogger(__name__)

#: Users with a profile row, and whatever images they hold. A LEFT JOIN because
#: only 19 of 43 dev users have a profile at all, and the avatar lives on
#: `users`' profile row while the banner lives on the same row — both nullable.
WITH_ASSETS = (
    select(User.id, User.email, Profile.avatar_url, Profile.banner_url)
    .join(Profile, Profile.user_id == User.id)
    .where(LIVE)
    .order_by(User.id)
)

# `target_user`, not `user_id`: SQLAlchemy reserves a bind parameter named after
# a column of the table being updated for its own SET clause, and the collision
# surfaces as a CompileError at execution rather than at construction.
SET_AVATAR = (
    update(Profile)
    .where(Profile.user_id == bindparam("target_user"))
    .values(avatar_url=bindparam("url"))
)

SET_BANNER = (
    update(Profile)
    .where(Profile.user_id == bindparam("target_user"))
    .values(banner_url=bindparam("url"))
)

#: Statements that read or write an existing ``users`` row, for the parity test.
#: The two `UPDATE`s target `user_profiles` keyed on `user_id`, which cascades
#: from `users` — they carry no `users` predicate because they never join it.
USER_STATEMENTS = (WITH_ASSETS,)


@dataclass(frozen=True, slots=True)
class ProfileAssets:
    """One user's images, as the migration sees them."""

    user_id: UUID
    email: str
    avatar_url: str | None
    banner_url: str | None

    def url_for(self, kind: AssetKind) -> str | None:
        return self.avatar_url if kind is AssetKind.AVATAR else self.banner_url


class AssetStore:
    """Reads what needs re-hosting and records where it went."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def profiles(self) -> list[ProfileAssets]:
        async with self._engine.connect() as connection:
            rows = (await connection.execute(WITH_ASSETS)).all()
        return [
            ProfileAssets(user_id=row[0], email=row[1], avatar_url=row[2], banner_url=row[3])
            for row in rows
        ]

    async def record(self, user_id: UUID, kind: AssetKind, url: str) -> bool:
        """Point the column at the re-hosted object.

        **The trigger is held off**, for the same reason provisioning holds it:
        ``user_profiles.updated_at`` carries Bubble's Modified Date, and moving a
        file is not a modification of the user's data. Without this, re-hosting
        rewrites every migrated profile timestamp to the run clock.
        """
        statement = SET_AVATAR if kind is AssetKind.AVATAR else SET_BANNER
        async with self._engine.begin() as connection:
            async with timestamps_from_source(connection, "user_profiles"):
                result = await connection.execute(statement, {"target_user": user_id, "url": url})
            return result.rowcount > 0


async def replace_url(
    session: AsyncSession,
    user_id: UUID,
    kind: AssetKind,
    url: str,
    *,
    focus: tuple[float, float] | None = None,
    looked: bool = False,
) -> str | None:
    """Point the column at a newly uploaded object; return what it held before.

    **The trigger is not held off here, and that is the difference from
    `AssetStore.record`.** Re-hosting a file is not a modification of the user's
    data, so the migration preserves Bubble's timestamp. A user changing their
    own photo *is* one, and `updated_at` should say so.

    The previous URL is read rather than returned by the `UPDATE`: PostgreSQL's
    `RETURNING` yields the new row, not the old one. The caller uses it to remove
    the image that was replaced — which is safe because object paths are keyed on
    the user as well as the content, so no two profiles share an object.

    **Upsert, for the same reason `upsert_profile` is one**: the profile row is
    created on first write, and `/me` reports `has_profile: false` until then. A
    plain `UPDATE` writes nothing for a user uploading a photo before they have
    ever saved a bio, and answers 200 having stored an object nothing points at.
    The two are pinned together by `test_first_write_creates_the_profile_row`,
    which drives both entry points against a user who has neither.
    """
    avatar = kind is AssetKind.AVATAR
    column = Profile.avatar_url if avatar else Profile.banner_url
    found = await session.execute(select(column).where(Profile.user_id == user_id))
    row = found.first()
    if row is None:
        values: dict[str, object] = {column.key: url}
        if avatar:
            values |= focus_columns(focus, looked=looked)
        await session.execute(insert(Profile).values(user_id=user_id, **values))
        return None

    previous: str | None = row[0]
    statement = SET_AVATAR if avatar else SET_BANNER
    await session.execute(statement, {"target_user": user_id, "url": url})
    # Only a different picture resets the focus. Object paths are content
    # hashes, so the same file uploaded again is the same URL — and a mentor's
    # chosen crop of it still holds.
    if avatar and previous != url:
        await session.execute(
            update(Profile)
            .where(Profile.user_id == user_id)
            .values(**focus_columns(focus, looked=looked))
        )
    return previous


async def clear_banner(session: AsyncSession, user_id: UUID) -> str | None:
    """Unset the banner; return the URL it held, or ``None`` if there was none.

    The design's "Remove image": the cover falls back to `cover_color` and
    `cover_art`. The row is locked while the old URL is read, so two removals
    cannot both report the same object for deletion — the second finds nothing.
    Does not commit; the caller deletes the object after it has.
    """
    found = await session.execute(
        select(Profile.banner_url).where(Profile.user_id == user_id).with_for_update()
    )
    previous = found.scalar_one_or_none()
    if previous is None:
        return None
    await session.execute(update(Profile).where(Profile.user_id == user_id).values(banner_url=None))
    return str(previous)


async def store_image(
    session: AsyncSession,
    storage: SupabaseStorage,
    kind: AssetKind,
    user_id: UUID,
    payload: bytes,
) -> tuple[str, str | None]:
    """Validate, re-encode, store, find the face, point the profile at it.

    **The one pipeline** for an image entering a profile: the upload endpoint
    and the demo seed both call it, so a demo avatar is processed, stored and
    focused exactly as a mentor's own upload is. Returns the new URL and the
    one it replaced. Does not commit.

    The blocking steps — decoding, detection, the synchronous storage client —
    run on worker threads, so a large photo does not stall other requests.
    The face is found in the **re-encoded** image, so the point matches the
    picture that is actually served.
    """
    image = await asyncio.to_thread(process, payload, kind)
    # **Before the upload**, so nothing detection does can leave a stored
    # object that no profile points at.
    focus, looked = None, False
    if kind is AssetKind.AVATAR:
        try:
            focus, looked = await asyncio.to_thread(avatar_focus, image.payload), True
        except FaceDetectionError:
            # Never a reason to refuse a photo. Left unprocessed for the backfill.
            logger.warning("face detection failed on upload", extra={"user_id": str(user_id)})
    path = object_path(user_id, kind, image.payload, image.content_type)
    url = await asyncio.to_thread(storage.upload, path, image.payload, image.content_type)
    previous = await replace_url(session, user_id, kind, url, focus=focus, looked=looked)
    return url, previous


async def stored_avatar_focus(session: AsyncSession, user_id: UUID) -> tuple[object, object]:
    """The avatar focus as stored now — `(None, None)` when there is none.

    Read back rather than taken from the detector, because what an upload
    leaves stored is not always what it detected: the same photo uploaded again
    keeps a mentor's chosen crop.
    """
    row = (
        await session.execute(
            select(Profile.avatar_focus_x, Profile.avatar_focus_y).where(Profile.user_id == user_id)
        )
    ).first()
    return (None, None) if row is None else (row[0], row[1])
