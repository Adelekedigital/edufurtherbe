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

from sqlalchemy import Select, exists, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from sqlalchemy.orm import aliased

from app.domain.handles import random_token
from app.domain.slugs import candidates, is_servable, name_slug, placeholder_slug
from app.infra.db.models.user import User
from app.infra.db.predicates import LIVE, restorable
from app.infra.db.triggers import timestamps_from_source

_UNIQUE_SLUG = "ix_users_slug_live"
#: Far beyond any real clash count; a loop that reaches it is a bug, not data.
_MAX_ATTEMPTS = 1000

#: Another account, for the restorable-holder check inside the UPDATE.
Held = aliased(User)

#: A session (requests) or a connection (the CLI scripts); both take savepoints.
Db = AsyncSession | AsyncConnection


def _random_placeholders() -> Iterator[str]:
    while True:
        yield placeholder_slug(random_token(8))


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
                        # Not a handle an account that may still restore holds.
                        ~exists().where(Held.slug == slug, *restorable(func.now(), Held)),
                        # A placeholder only fills a missing handle; a name's
                        # handle may also replace a placeholder.
                        User.slug.is_(None)
                        if placeholder
                        else or_(User.slug.is_(None), User.slug_is_placeholder.is_(True)),
                    )
                    .values(slug=slug, slug_is_placeholder=placeholder)
                    .returning(User.slug)
                )
                claimed = written.scalar_one_or_none()
            if claimed is not None:
                return claimed
            # Nothing written: this account's handle is final (done), or a
            # restorable account holds this option (try the next one).
            if not await _still_open(session, user_id, placeholder=placeholder):
                return None
        except IntegrityError as exc:
            if _UNIQUE_SLUG not in str(exc.orig):
                raise
    return None  # pragma: no cover - `candidates` and the random stream never end


async def _still_open(session: Db, user_id: UUID, *, placeholder: bool) -> bool:
    """May this account still take ``placeholder``'s kind of handle?"""
    row = (
        await session.execute(select(User.slug, User.slug_is_placeholder).where(User.id == user_id))
    ).one_or_none()
    if row is None:
        return False
    return row.slug is None if placeholder else not _is_final(row.slug, row.slug_is_placeholder)


async def give_placeholder(session: Db, user_id: UUID) -> str | None:
    """A `user-…` handle for an account that has none yet. Does not commit."""
    return await _claim(session, user_id, _random_placeholders(), placeholder=True)


async def _name_handle(
    session: Db, user_id: UUID, first_name: str | None, last_name: str | None
) -> str | None:
    base = name_slug(first_name, last_name)
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
        await session.execute(
            select(User.first_name, User.last_name, User.slug, User.slug_is_placeholder).where(
                User.id == user_id
            )
        )
    ).one_or_none()
    if row is None or _is_final(row.slug, row.slug_is_placeholder):
        return None
    return await _name_handle(session, user_id, row.first_name, row.last_name)


async def give_slug(session: Db, user_id: UUID) -> str | None:
    """The account's handle from its name, else a placeholder; nothing if it
    already has a final one. Does not commit."""
    named = await settle_name_slug(session, user_id)
    # `give_placeholder` writes only over a missing handle, so a final one or
    # an existing placeholder is left alone.
    return named if named is not None else await give_placeholder(session, user_id)


def _is_final(slug: str | None, placeholder: bool) -> bool:
    """A shortcut only; the UPDATE's own WHERE is what guarantees it."""
    return slug is not None and not placeholder


def _missing(
    *, legacy_only: bool = False
) -> Select[tuple[UUID, str | None, str | None, str | None]]:
    """Live accounts with no handle, or a placeholder one a name may now settle."""
    query = select(User.id, User.first_name, User.last_name, User.slug).where(
        or_(User.slug.is_(None), User.slug_is_placeholder.is_(True)), LIVE
    )
    if legacy_only:
        query = query.where(User.legacy_bubble_id.is_not(None))
    return query.order_by(User.id)


async def give_missing_slugs(session: Db, *, legacy_only: bool = False) -> tuple[int, int]:
    """A handle for every live account without one, and a name's handle for
    one on a placeholder whose name is now known (only imported accounts with
    ``legacy_only``): ``(from names, placeholders)``. Does not commit, and does
    not hold `updated_at`: the caller does (the backfill, or the loader, which
    already holds it)."""
    from_name = placeholders = 0
    rows = (await session.execute(_missing(legacy_only=legacy_only))).all()
    for user_id, first_name, last_name, slug in rows:
        if await _name_handle(session, user_id, first_name, last_name):
            from_name += 1
        elif slug is None and await give_placeholder(session, user_id):
            placeholders += 1
    return from_name, placeholders


@dataclass(frozen=True)
class SlugBackfill:
    """What a backfill gave out, and existing handles the router cannot serve."""

    from_name: int
    placeholders: int
    unservable: tuple[str, ...]


async def _connection(session: Db) -> AsyncConnection:
    return await session.connection() if isinstance(session, AsyncSession) else session


async def backfill_slugs(session: Db, *, apply: bool) -> SlugBackfill:
    """A handle for every live account without one: from its name, else a
    placeholder. Existing handles, imported ones included, are never touched,
    and neither is `updated_at`: a migrated row's carries the legacy Modified
    Date, and giving it a handle is not an edit. Does not commit.

    Without ``apply`` it only counts and writes nothing. With it, the trigger
    hold takes an exclusive lock on ``users`` until the caller commits, so it
    gives up after 3 s rather than queue live traffic behind it: run it in a
    quiet window (the cutover's, or a quiet moment on dev)."""
    if not apply:
        missing = (await session.execute(_missing())).all()
        from_name = sum(1 for _, first, last, _slug in missing if name_slug(first, last))
        placeholders = sum(
            1 for _, first, last, slug in missing if slug is None and not name_slug(first, last)
        )
    else:
        connection = await _connection(session)
        await connection.execute(text("SET LOCAL lock_timeout = '3s'"))
        async with timestamps_from_source(connection, "users"):
            from_name, placeholders = await give_missing_slugs(session)
    handles = (
        (await session.execute(select(User.slug).where(User.slug.is_not(None), LIVE)))
        .scalars()
        .all()
    )
    unservable = tuple(sorted(slug for slug in handles if slug and not is_servable(slug)))
    return SlugBackfill(from_name, placeholders, unservable)
