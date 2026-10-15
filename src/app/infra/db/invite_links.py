"""Personal invite links: creating one, picking its handle, and resolving any code.

Owner decisions of 2026-10-15 (settled decisions 254-256). Each user has at most
one permanent link (`invite_links`). It carries a random code until its owner
picks a handle. Only the current code resolves. A code the link stopped
carrying goes to `invite_link_retired_codes` and is held from everyone else for
`RETIRED_CODE_HOLD`.

ONE RESOLVER
============
:func:`resolve_code` is the one statement of which invite a code names. Claim
and the public landing lookup both call it, so they cannot disagree.
**Single-use `referrals.code` is looked up first, then the current
`invite_links.code`.** A handle equal to a referral code is refused when it is
picked, and a random code colliding with either is a 128-bit event, so the
order only matters in theory. It is still the order, and it is written down.

NOTHING HERE COMMITS
====================
The caller does, as in `referral_writer`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import exists, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.domain.referrals import RETIRED_CODE_HOLD, make_code
from app.infra.db.models.referrals import InviteLink, InviteLinkRetiredCode, Referral
from app.infra.db.models.user import User, UserProfile
from app.infra.db.predicates import LIVE

__all__ = [
    "INVITE_CODE_LOCK_NAMESPACE",
    "ResolvedCode",
    "ensure_invite_link",
    "inviter_of",
    "pick_handle",
    "resolve_code",
]

#: ``'INVT'`` as four ASCII bytes. Its own namespace in the one global keyspace
#: `pg_advisory_xact_lock` shares with the credit, slot, limit and image locks.
INVITE_CODE_LOCK_NAMESPACE = 0x494E5654


@dataclass(frozen=True)
class ResolvedCode:
    """The invite a code names.

    ``referral_id`` is set for a single-use code and ``None`` for a personal
    link. ``owner_live`` is whether the referrer or link owner still exists.
    """

    owner_id: UUID
    owner_live: bool
    referral_id: UUID | None
    invitee_user_id: UUID | None
    first_name: str | None
    avatar_url: str | None


def _owner_columns() -> tuple[Any, ...]:
    return (LIVE.label("owner_live"), User.first_name, UserProfile.avatar_url)


async def resolve_code(session: AsyncSession, code: str) -> ResolvedCode | None:
    """The invite ``code`` names: a single-use referral first, then a current link.

    A retired code is in neither place, so it resolves exactly as a code that
    never existed did. A miss always runs both statements.
    """
    referral = (
        await session.execute(
            select(Referral.id, Referral.referrer_id, Referral.invitee_user_id, *_owner_columns())
            .join(User, User.id == Referral.referrer_id)
            .outerjoin(UserProfile, UserProfile.user_id == User.id)
            .where(Referral.code == code)
        )
    ).one_or_none()
    if referral is not None:
        return ResolvedCode(
            owner_id=referral.referrer_id,
            owner_live=referral.owner_live,
            referral_id=referral.id,
            invitee_user_id=referral.invitee_user_id,
            first_name=referral.first_name,
            avatar_url=referral.avatar_url,
        )

    link = (
        await session.execute(
            select(InviteLink.user_id, *_owner_columns())
            .join(User, User.id == InviteLink.user_id)
            .outerjoin(UserProfile, UserProfile.user_id == User.id)
            .where(InviteLink.code == code)
        )
    ).one_or_none()
    if link is None:
        return None
    return ResolvedCode(
        owner_id=link.user_id,
        owner_live=link.owner_live,
        referral_id=None,
        invitee_user_id=None,
        first_name=link.first_name,
        avatar_url=link.avatar_url,
    )


async def inviter_of(session: AsyncSession, code: str) -> ResolvedCode | None:
    """Who a landing page may name for ``code``, or ``None`` (decision 256).

    ``None`` for anything that cannot be joined through: unknown, retired, a
    single-use code somebody already claimed, or an owner who was deleted. One
    answer for all of them, so the public cannot tell them apart.
    """
    resolved = await resolve_code(session, code)
    if resolved is None or not resolved.owner_live:
        return None
    if resolved.referral_id is not None and resolved.invitee_user_id is not None:
        return None
    return resolved


async def ensure_invite_link(session: AsyncSession, user_id: UUID) -> tuple[str, bool]:
    """The caller's link code, creating the link first if needed.

    Returns ``(code, created)``. **The insert arbitrates**: two first requests
    both insert, `UNIQUE (user_id)` lets one through, and ``DO NOTHING`` hands
    the other no row, so it reads the winner's. Under READ COMMITTED that read
    is a new statement and sees the committed row.
    """
    created = (
        await session.execute(
            pg_insert(InviteLink)
            .values(user_id=user_id, code=make_code())
            .on_conflict_do_nothing(index_elements=["user_id"])
            .returning(InviteLink.code)
        )
    ).scalar_one_or_none()
    if created is not None:
        return created, True
    # The conflict that skipped the insert is this row, so it is there to read.
    existing = (
        await session.execute(select(InviteLink.code).where(InviteLink.user_id == user_id))
    ).scalar_one()
    return existing, False


async def _lock_codes(session: AsyncSession, *codes: str) -> None:
    """Serialise every write that gives up or takes one of ``codes``.

    **Sorted**, so two changes crossing each other's codes take the locks in the
    same order and cannot deadlock. Held until the transaction ends.
    """
    for code in sorted(set(codes)):
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:ns, hashtext(:key))"),
            {"ns": INVITE_CODE_LOCK_NAMESPACE, "key": code},
        )


async def pick_handle(session: AsyncSession, user_id: UUID, handle: str) -> str:
    """Point the caller's link at ``handle``, retiring the code it carried.

    ``handle`` is already normalised and well formed (the domain's rule, checked
    at the boundary). Raises :class:`ConflictError` when it is taken: another
    link's current code, a single-use referral code, or a code another link
    retired inside the hold. Picking the handle you already carry changes
    nothing.

    **Why a lock and not only the unique index.** The index stops two links
    carrying one code, but not a stranger picking a handle whose owner is moving
    off it at that moment: the stranger's hold check runs before the retirement
    commits, finds nothing, and the index then lets them in as soon as the
    owner's row gives the code up. Locking both the old and the new code puts
    the hold check after any retirement of the same code. It also makes two
    picks of one free handle a 409 rather than a unique violation.
    """
    await ensure_invite_link(session, user_id)
    link = (
        await session.execute(
            select(InviteLink.id, InviteLink.code)
            .where(InviteLink.user_id == user_id)
            .with_for_update()
        )
    ).one()
    if link.code == handle:
        return handle

    await _lock_codes(session, link.code, handle)

    taken = await session.scalar(
        select(
            exists().where(InviteLink.code == handle)
            | exists().where(Referral.code == handle)
            | exists().where(
                InviteLinkRetiredCode.code == handle,
                InviteLinkRetiredCode.invite_link_id != link.id,
                InviteLinkRetiredCode.created_at > func.now() - RETIRED_CODE_HOLD,
            )
        )
    )
    if taken:
        raise ConflictError("that handle is taken")

    await session.execute(update(InviteLink).where(InviteLink.id == link.id).values(code=handle))
    await session.execute(
        pg_insert(InviteLinkRetiredCode).values(invite_link_id=link.id, code=link.code)
    )
    return handle
