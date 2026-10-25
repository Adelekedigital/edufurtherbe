"""A mentor's video options: read and set the default, and find the row an
offering is pinned to.

**The only writer `mentor_conferencing_options` has** (#124 shipped the table
with none): the default through `/me/conferencing` (#224), and a non-default row
for an offering's own venue (decision 288). Both go through one upsert on
`(user_id, provider)`. The three connection columns are not touched: no provider
authenticates yet, and they are "declared now, written by nothing" (#21).

**Scoped to the caller's live mentor profile in the query**, through
`mentor_exists()` — a soft-deleted profile or account answers as "no mentor",
never as an empty default.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import Insert, insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ConferencingProvider, OptionSource
from app.infra.db.models.mentoring import MentorConferencingOption as Option
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.user import User
from app.infra.db.public_visibility import mentor_exists

__all__ = ["offering_option", "own_default_option", "set_default_option"]


def _own_profile(user_id: UUID) -> Any:
    """The caller's mentor profile, if it and the account are live."""
    return (
        select(MentorProfile.user_id)
        .join(User, User.id == MentorProfile.user_id)
        .where(MentorProfile.user_id == user_id, *mentor_exists())
    )


async def own_default_option(
    session: AsyncSession, user_id: UUID
) -> tuple[bool, dict[str, Any] | None]:
    """Whether the caller is a mentor, and their saved default (``None`` if never
    chosen)."""
    found = await session.execute(
        _own_profile(user_id)
        .add_columns(Option.provider, Option.custom_url)
        .outerjoin(Option, (Option.user_id == MentorProfile.user_id) & Option.is_default)
    )
    row = found.mappings().one_or_none()
    if row is None:
        return False, None
    if row["provider"] is None:
        return True, None
    return True, {"provider": row["provider"], "custom_url": row["custom_url"]}


async def set_default_option(
    session: AsyncSession,
    user_id: UUID,
    provider: ConferencingProvider,
    custom_url: str | None,
) -> bool:
    """Make ``provider`` the caller's default. ``False`` if they are not a mentor.

    **Clear, then set**, in one transaction, under a lock on the profile row: the
    partial unique index allows one default per mentor, so the old default must
    stop being one before the new one starts, and two concurrent switches must
    not both clear and both set. The option row is upserted on
    `(user_id, provider)`, so a mentor returning to a provider reuses its row and
    a new personal link replaces the old one.
    """
    locked = await session.execute(_own_profile(user_id).with_for_update(of=MentorProfile))
    if locked.first() is None:
        return False
    await session.execute(
        update(Option).where(Option.user_id == user_id, Option.is_default).values(is_default=False)
    )
    await session.execute(_upsert(user_id, provider, custom_url, default=True))
    return True


async def offering_option(
    session: AsyncSession, user_id: UUID, provider: ConferencingProvider
) -> UUID:
    """The id of this mentor's row for ``provider``, created if missing (decision 288).

    **Never the default**: a new row is inserted with `is_default = false`, and
    an existing one keeps whatever it was, so pinning an offering cannot change
    where the mentor's other offerings are held. **`FOR KEY SHARE` on the
    profile first**, the lock the insert's foreign key takes anyway: taken
    after the insert, a concurrent `set_default_option` (holding `FOR UPDATE`
    and then upserting the same provider) deadlocks with it. Not `FOR UPDATE`:
    this never touches `is_default`, so it need not serialise with other pins.
    The caller has already scoped the offering to ``user_id``.
    """
    await session.execute(_own_profile(user_id).with_for_update(of=MentorProfile, key_share=True))
    statement = _upsert(user_id, provider, None, default=False).returning(Option.id)
    return (await session.execute(statement)).scalar_one()


def _upsert(
    user_id: UUID, provider: ConferencingProvider, custom_url: str | None, *, default: bool
) -> Insert:
    """The one upsert on `(user_id, provider)`. Either way the row becomes the
    mentor's own (`source`), so a cleanup of imported rows cannot take back a
    choice; only a default write changes `is_default` or the link."""
    statement = insert(Option).values(
        user_id=user_id,
        provider=provider,
        custom_url=custom_url,
        is_default=default,
        source=OptionSource.MENTOR,
    )
    chosen: dict[str, Any] = {"source": OptionSource.MENTOR}
    if default:
        chosen |= {"custom_url": statement.excluded.custom_url, "is_default": True}
    return statement.on_conflict_do_update(
        constraint="uq_mentor_conferencing_options_user_id_provider", set_=chosen
    )
