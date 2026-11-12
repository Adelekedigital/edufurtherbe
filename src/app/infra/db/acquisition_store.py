"""Where an account came from, recorded once (PRD 30 PR 2, decision 329).

**Write-once in the statement**: the `UPDATE` matches only while
`acquired_at` is null and the account is not `migrated`, so a second call, a
racing one or a migrated account changes nothing and reads back what is there.
Coercion is `domain.attribution`'s; an invite is the one `resolve_code` finds,
the lookup the claim and the landing page use. The code itself is never
stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.attribution import signup_channel, signup_source
from app.domain.enums import SignupChannel, SignupSource
from app.infra.db.invite_links import resolve_code
from app.infra.db.models.user import User
from app.infra.db.predicates import migrated

__all__ = ["Acquisition", "record_acquisition"]


@dataclass(frozen=True, slots=True)
class Acquisition:
    """What is recorded, and whether this call recorded it."""

    source: SignupSource
    channel: SignupChannel | None
    recorded: bool


async def _invited(session: AsyncSession, user_id: UUID, code: str | None) -> bool:
    """Whether ``code`` names a live invite of somebody else's."""
    if not code:
        return False
    resolved = await resolve_code(session, code)
    return resolved is not None and resolved.owner_live and resolved.owner_id != user_id


async def record_acquisition(
    session: AsyncSession,
    user_id: UUID,
    *,
    source: object,
    channel: object,
    referral_code: str | None,
) -> Acquisition:
    """Record the caller's acquisition unless one is already there. Does not commit."""
    invited = await _invited(session, user_id, referral_code)
    written = (
        await session.execute(
            update(User)
            .where(
                User.id == user_id,
                User.acquired_at.is_(None),
                ~migrated(User),
            )
            .values(
                signup_source=signup_source(source, invited=invited),
                signup_channel=signup_channel(channel),
                acquired_at=func.now(),
            )
            .returning(User.signup_source, User.signup_channel)
        )
    ).first()
    if written is not None:
        return Acquisition(written.signup_source, written.signup_channel, recorded=True)
    kept = (
        await session.execute(
            select(User.signup_source, User.signup_channel).where(User.id == user_id)
        )
    ).one()
    return Acquisition(kept.signup_source, kept.signup_channel, recorded=False)
