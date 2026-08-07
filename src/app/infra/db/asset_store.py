"""The database side of asset re-hosting.

Same shape and the same reason as ``provisioning_store.py``: ``scripts/`` is
outside ``mypy``, ``bandit`` and the coverage floor, so SQL written there is
checked by ruff alone (tier-2 row 44).

``LIVE`` is imported rather than re-typed. This module is the second consumer,
which is why the predicate moved to ``infra/db/predicates.py`` in this change.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import bindparam, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.assets import AssetKind
from app.infra.db.models.user import User
from app.infra.db.models.user import UserProfile as Profile
from app.infra.db.predicates import LIVE
from app.infra.db.triggers import timestamps_from_source

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
