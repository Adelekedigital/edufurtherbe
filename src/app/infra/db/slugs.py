"""Give accounts their public handle (`domain.slugs`), the database deciding clashes.

Uniqueness among live accounts is ``ix_users_slug_live`` (lowercase by the
CHECK), so a clash is learned from the index, never by looking first: each
attempt runs in a savepoint and the next candidate is tried on a unique
violation. A handle a self-deleted account still holds is skipped too: it can
restore, and its handle must still be free when it does. A handle that is
final (`slug_is_placeholder` false) is never written over.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import exists, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.domain.handles import random_token
from app.domain.slugs import candidates, is_servable, name_slug, placeholder_slug
from app.infra.db.models.user import User
from app.infra.db.predicates import LIVE
from app.infra.db.triggers import timestamps_from_source

_UNIQUE_SLUG = "ix_users_slug_live"
#: Far beyond any real clash count; a loop that reaches it is a bug, not data.
_MAX_ATTEMPTS = 1000

#: A session (requests) or a connection (the CLI scripts); both take savepoints.
Db = AsyncSession | AsyncConnection


def _random_placeholders() -> Iterator[str]:
    while True:
        yield placeholder_slug(random_token(8))


async def _held_by_a_deleted_account(session: Db, slug: str) -> bool:
    return bool(
        await session.scalar(
            select(exists().where(User.slug == slug, User.deleted_at.is_not(None)))
        )
    )


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
        if await _held_by_a_deleted_account(session, slug):
            continue
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


async def _name_handle(
    session: Db, user_id: UUID, first_name: str | None, last_name: str | None
) -> str | None:
    base = name_slug(first_name, last_name) if first_name else None
    if base is None:
        return None
    return await _claim(session, user_id, candidates(base), placeholder=False)


async def settle_name_slug(session: Db, user_id: UUID) -> str | None:
    """Replace a missing or placeholder handle, once, by one from the saved name.

    Does nothing when the handle is final (the update's own WHERE says so), or
    when there is no first name or it gives nothing usable (the placeholder
    stays, and the next name save tries again). Does not commit; the caller's
    transaction holds the name and the handle together.
    """
    row = (
        await session.execute(select(User.first_name, User.last_name).where(User.id == user_id))
    ).one_or_none()
    if row is None:
        return None
    return await _name_handle(session, user_id, row.first_name, row.last_name)


async def give_slug(session: Db, user_id: UUID) -> str | None:
    """The account's handle from its name, else a placeholder; nothing if it
    already has a final one. Does not commit."""
    return await settle_name_slug(session, user_id) or await give_placeholder(session, user_id)


@dataclass(frozen=True)
class SlugBackfill:
    """What a backfill gave out, and existing handles the router cannot serve."""

    from_name: int
    placeholders: int
    unservable: tuple[str, ...]


async def _connection(session: Db) -> AsyncConnection:
    return await session.connection() if isinstance(session, AsyncSession) else session


async def backfill_slugs(session: Db) -> SlugBackfill:
    """A handle for every live account without one: from its name, else a
    placeholder. Existing handles, imported ones included, are never touched,
    and neither is `updated_at`: a migrated row's carries the legacy Modified
    Date, and giving it a handle is not an edit. Does not commit; a dry run is
    the caller's rollback."""
    missing = (
        await session.execute(
            select(User.id, User.first_name, User.last_name)
            .where(User.slug.is_(None), LIVE)
            .order_by(User.id)
        )
    ).all()
    from_name = placeholders = 0
    async with timestamps_from_source(await _connection(session), "users"):
        for user_id, first_name, last_name in missing:
            if await _name_handle(session, user_id, first_name, last_name):
                from_name += 1
            elif await give_placeholder(session, user_id):
                placeholders += 1
    handles = (
        (await session.execute(select(User.slug).where(User.slug.is_not(None), LIVE)))
        .scalars()
        .all()
    )
    unservable = tuple(sorted(slug for slug in handles if slug and not is_servable(slug)))
    return SlugBackfill(from_name, placeholders, unservable)
