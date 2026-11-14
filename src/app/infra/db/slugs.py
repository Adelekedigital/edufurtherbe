"""Give accounts their public handle (`domain.slugs`), the database deciding clashes.

Uniqueness is ``ix_users_slug_live`` (live rows; lowercase by the CHECK), so a
clash is learned from the index, never by looking first: each attempt runs in a
savepoint and the next candidate is tried on a unique violation. A handle that
is final (`slug_is_placeholder` false) is never written over.
"""

from __future__ import annotations

import logging
import secrets
import string
from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.domain.slugs import HANDLE_PATTERN, candidates, name_slug, placeholder_slug
from app.infra.db.models.user import User
from app.infra.db.predicates import LIVE

logger = logging.getLogger(__name__)

_BASE36 = string.digits + string.ascii_lowercase
_UNIQUE_SLUG = "ix_users_slug_live"
#: Far beyond any real clash count; a loop that reaches it is a bug, not data.
_MAX_ATTEMPTS = 1000

#: A session (requests) or a connection (the CLI scripts); both take savepoints.
Db = AsyncSession | AsyncConnection


def _random_placeholders() -> Iterable[str]:
    while True:
        yield placeholder_slug("".join(secrets.choice(_BASE36) for _ in range(8)))


async def _claim(
    session: Db, user_id: UUID, options: Iterable[str], *, placeholder: bool
) -> str | None:
    """Write the first free option: a placeholder over a missing handle, a
    name's handle over a missing or placeholder one.

    ``None`` when there is nothing to write over.
    """
    for attempt, slug in enumerate(options):
        if attempt >= _MAX_ATTEMPTS:
            raise RuntimeError("no free handle after 1000 attempts")
        try:
            async with session.begin_nested():
                written = await session.execute(
                    update(User)
                    .where(
                        User.id == user_id,
                        # A placeholder only fills a missing handle; a name's
                        # handle may also replace a placeholder.
                        User.slug.is_(None)
                        if placeholder
                        else or_(User.slug.is_(None), User.slug_is_placeholder.is_(True)),
                    )
                    .values(slug=slug, slug_is_placeholder=placeholder)
                    .returning(User.slug)
                )
                return written.scalar_one_or_none()
        except IntegrityError as exc:
            if _UNIQUE_SLUG not in str(exc.orig):
                raise
    return None  # pragma: no cover - `candidates` and the random stream never end


async def give_placeholder(session: Db, user_id: UUID) -> str | None:
    """A `user-…` handle for an account that has none yet. Does not commit."""
    return await _claim(session, user_id, _random_placeholders(), placeholder=True)


async def settle_name_slug(session: Db, user_id: UUID) -> str | None:
    """Replace a placeholder, once, by a handle made from the saved name.

    Does nothing when the handle is final (the update's own WHERE says so) or
    the name gives nothing usable (the placeholder stays, and the next name
    save tries again). Does not
    commit; the caller's transaction holds the name and the handle together.
    """
    row = (
        await session.execute(select(User.first_name, User.last_name).where(User.id == user_id))
    ).one_or_none()
    if row is None or not row.first_name:
        return None
    base = name_slug(row.first_name, row.last_name)
    if base is None:
        return None
    return await _claim(session, user_id, candidates(base), placeholder=False)


async def give_slug(session: Db, user_id: UUID) -> str | None:
    """The account's handle from its name, else a placeholder; nothing if it
    already has a final one. Does not commit."""
    return await settle_name_slug(session, user_id) or await give_placeholder(session, user_id)


@dataclass(frozen=True)
class SlugBackfill:
    """What a backfill gave out, and imported handles the frontend would refuse."""

    from_name: int
    placeholders: int
    off_pattern: tuple[str, ...]


async def backfill_slugs(session: Db) -> SlugBackfill:
    """A handle for every live account without one: from its name, else a
    placeholder. Existing handles, imported ones included, are never touched.
    Does not commit; a dry run is the caller's rollback."""
    missing = (
        (await session.execute(select(User.id).where(User.slug.is_(None), LIVE).order_by(User.id)))
        .scalars()
        .all()
    )
    from_name = placeholders = 0
    for user_id in missing:
        if await settle_name_slug(session, user_id):
            from_name += 1
        elif await give_placeholder(session, user_id):
            placeholders += 1
    handles = (
        (await session.execute(select(User.slug).where(User.slug.is_not(None), LIVE)))
        .scalars()
        .all()
    )
    off_pattern = tuple(sorted(slug for slug in handles if slug and not HANDLE_PATTERN.match(slug)))
    return SlugBackfill(from_name, placeholders, off_pattern)
