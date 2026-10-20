"""A user's own links: the reader and the one writer (decision 275).

**Every write goes through here**, the settings endpoints and the profile
PATCH's legacy `social_twitter`/`social_youtube` alike, so the one-per-platform
rule, the three-website count and the old-column contract live in one place.

**The old columns.** Until they are dropped, writing or deleting an X or
YouTube link also clears that platform's `user_profiles` column. That is what
makes the catch-up copy safe to run and the downgrade correct: a column holding
a value with no row can only be one written before this release, never a link
somebody deleted. See `d6f2a9c4e185` and `c1e5a8d3f027`.

**Serialised per user** by an advisory lock taken before anything is read, so
two requests cannot both pass the website count. One-per-platform is the partial
unique index's alone, with its violation answered as a `409` (#169), and the
value is checked here for both the create and the change, once.

Scoped to the caller in every statement (non-negotiable #5): a link id that is
somebody else's is the same `None` as one that does not exist.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import LinkPlatformTakenError, ValidationError, WebsiteLimitReachedError
from app.domain.enums import LinkPlatform
from app.domain.social_links import (
    LEGACY_LINK_FIELDS,
    MAX_WEBSITES,
    Link,
    SocialNetwork,
    canonical_social,
    handle_of,
    owned_link,
    stored_link,
)
from app.infra.db.models.user import UserLink, UserProfile

__all__ = [
    "LINK_LOCK_NAMESPACE",
    "add_link",
    "canonical_or_refuse",
    "change_link",
    "delete_link",
    "links_of",
    "set_platform_link",
]

#: Namespace for this module's advisory lock: `'LINK'` as four ASCII bytes.
LINK_LOCK_NAMESPACE = 0x4C494E4B

_PLATFORM_TAKEN = "You already have a link on this platform. Edit or delete it instead."
_WEBSITE_LIMIT = f"You can list up to {MAX_WEBSITES} websites."
_UNIQUE_INDEX = "ix_user_links_one_per_platform"


async def links_of(session: AsyncSession, user_id: UUID, *, published: bool) -> list[Link]:
    """The user's links in their order, then oldest first.

    `published` is the public profile's read: only links the rule can read.
    The owner's read (`published=False`) also returns one it no longer can, as
    stored, because it still holds its platform's slot and must stay deletable.
    The caller decides whether this user may be shown (`LIVE`, the public
    profile's visibility); this reads one user's rows and nobody else's.
    """
    rows = await session.execute(
        select(UserLink.id, UserLink.platform, UserLink.url, UserLink.position)
        .where(UserLink.user_id == user_id)
        .order_by(UserLink.position, UserLink.created_at, UserLink.id)
    )
    if not published:
        return [owned_link(r.id, str(r.platform), r.url, r.position) for r in rows]
    return [
        link
        for r in rows
        if (link := stored_link(r.id, str(r.platform), r.url, r.position)) is not None
    ]


def canonical_or_refuse(platform: LinkPlatform, value: str) -> str:
    """The canonical link for `value`, or a `422` at `/value`."""
    url = canonical_social(SocialNetwork(platform.value), value)
    if url is None:
        raise ValidationError(
            f"That is not a {platform.value} link we can use.",
            field_errors=(("/value", f"Not a valid {platform.value} handle or link."),),
        )
    return url


async def _lock(session: AsyncSession, user_id: UUID) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:ns, hashtext(:key))"),
        {"ns": LINK_LOCK_NAMESPACE, "key": str(user_id)},
    )


async def _clear_legacy(session: AsyncSession, user_id: UUID, platform: LinkPlatform) -> None:
    column = LEGACY_LINK_FIELDS.get(platform)
    if column is not None:
        await session.execute(
            update(UserProfile).where(UserProfile.user_id == user_id).values({column: None})
        )


async def _refuse_if_full(session: AsyncSession, user_id: UUID, platform: LinkPlatform) -> None:
    """The website count. One-per-platform is the unique index's alone: a second
    X link fails the insert, which `add_link` answers as the same `409`."""
    if platform is not LinkPlatform.WEBSITE:
        return
    held = await session.scalar(
        select(func.count())
        .select_from(UserLink)
        .where(UserLink.user_id == user_id, UserLink.platform == platform)
    )
    if (held or 0) >= MAX_WEBSITES:
        raise WebsiteLimitReachedError(_WEBSITE_LIMIT, limit=MAX_WEBSITES)


async def add_link(
    session: AsyncSession, user_id: UUID, platform: LinkPlatform, value: str
) -> Link:
    """Add a link at the end of the user's list. Does not commit."""
    url = canonical_or_refuse(platform, value)
    await _lock(session, user_id)
    await _refuse_if_full(session, user_id, platform)
    link_id, position = await _insert(session, user_id, platform, url)
    await _clear_legacy(session, user_id, platform)
    return Link(
        id=link_id,
        platform=platform.value,
        url=url,
        value=handle_of(SocialNetwork(platform.value), url),
        position=position,
    )


async def _insert(
    session: AsyncSession, user_id: UUID, platform: LinkPlatform, url: str
) -> tuple[UUID, int]:
    """Append a row at the end of the user's order: the one insert. A second
    link on a platform fails the unique index, answered as a `409` (#169)."""
    position = await session.scalar(
        select(func.coalesce(func.max(UserLink.position) + 1, 0)).where(UserLink.user_id == user_id)
    )
    try:
        async with session.begin_nested():
            link_id = await session.scalar(
                insert(UserLink)
                .values(user_id=user_id, platform=platform.value, url=url, position=position)
                .returning(UserLink.id)
            )
    except IntegrityError as exc:
        if _UNIQUE_INDEX in str(exc.orig):
            raise LinkPlatformTakenError(_PLATFORM_TAKEN) from exc
        raise
    return UUID(str(link_id)), int(position or 0)


async def change_link(
    session: AsyncSession, user_id: UUID, link_id: UUID, value: str
) -> Link | None:
    """Change a link's value; the platform is fixed. `None` when the caller has
    no such link. Does not commit."""
    await _lock(session, user_id)
    platform = await session.scalar(
        select(UserLink.platform).where(UserLink.id == link_id, UserLink.user_id == user_id)
    )
    if platform is None:
        return None
    url = canonical_or_refuse(LinkPlatform(str(platform)), value)
    position = await session.scalar(
        update(UserLink)
        .where(UserLink.id == link_id, UserLink.user_id == user_id)
        .values(url=url)
        .returning(UserLink.position)
    )
    await _clear_legacy(session, user_id, LinkPlatform(str(platform)))
    return stored_link(link_id, str(platform), url, int(position or 0))


async def delete_link(session: AsyncSession, user_id: UUID, link_id: UUID) -> bool:
    """Delete one of the caller's links. `False` when they have no such link."""
    await _lock(session, user_id)
    platform = await session.scalar(
        delete(UserLink)
        .where(UserLink.id == link_id, UserLink.user_id == user_id)
        .returning(UserLink.platform)
    )
    if platform is None:
        return False
    await _clear_legacy(session, user_id, LinkPlatform(str(platform)))
    return True


async def set_platform_link(
    session: AsyncSession, user_id: UUID, platform: LinkPlatform, url: str | None
) -> None:
    """The profile PATCH's legacy `social_twitter`/`social_youtube`: set the one
    link on `platform` to an already-canonical `url`, or remove it on `None`.

    For one release only, so a client still sending the old fields keeps
    working; the same lock and the same column contract as the endpoints.
    """
    await _lock(session, user_id)
    existing = await session.scalar(
        select(UserLink.id).where(UserLink.user_id == user_id, UserLink.platform == platform)
    )
    if url is None:
        if existing is not None:
            await session.execute(delete(UserLink).where(UserLink.id == existing))
    elif existing is not None:
        await session.execute(update(UserLink).where(UserLink.id == existing).values(url=url))
    else:
        await _insert(session, user_id, platform, url)
    await _clear_legacy(session, user_id, platform)
