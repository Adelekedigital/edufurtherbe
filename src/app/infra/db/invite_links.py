"""Personal invite links: creating one, its handle, revoking it, and resolving any code.

Owner decisions of 2026-10-15 (settled decisions 254-256). Each user has at most
one permanent link (`invite_links`). It starts with a default code built from
their first name (`domain.referrals.default_code`) and keeps it until its owner
picks a handle. Only the current code of an active link resolves. A code the
link stopped carrying goes to `invite_link_retired_codes` and is held from
everyone else for `RETIRED_CODE_HOLD`. A revoked link resolves for nobody, but
its code stays its own.

ONE RESOLVER
============
:func:`resolve_code` is the one statement of which invite a code names, revoked
links included. Claim and the public landing lookup both call it, so they cannot
disagree. **Single-use `referrals.code` is looked up first, then the current
code of an active `invite_links` row.** A link code is never allowed to equal a
referral code (:func:`_code_taken`), and a newly generated referral code
colliding with a link is a 128-bit event, so the order only matters in theory.
It is still the order, and it is written down.

ONE AVAILABILITY RULE
=====================
:func:`_code_taken` decides whether a code may become a link's: not another
link's current code (revoked or not), not a single-use referral code, and not a
code another link retired inside the hold. A default code and a picked handle
both pass through it, under the same advisory lock.

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
from app.domain.referrals import (
    DEFAULT_CODE_SUFFIXES,
    RETIRED_CODE_HOLD,
    default_code,
    is_plausible_code,
    normalise_handle,
)
from app.infra.db.models.referrals import InviteLink, InviteLinkRetiredCode, Referral
from app.infra.db.models.user import User, UserProfile
from app.infra.db.predicates import LIVE

__all__ = [
    "INVITE_CODE_LOCK_NAMESPACE",
    "LinkState",
    "ResolvedCode",
    "ensure_invite_link",
    "inviter_of",
    "pick_handle",
    "resolve_code",
    "set_link_active",
]

#: ``'INVT'`` as four ASCII bytes. Its own namespace in the one global keyspace
#: `pg_advisory_xact_lock` shares with the credit, slot, limit and image locks.
INVITE_CODE_LOCK_NAMESPACE = 0x494E5654

HANDLE_TAKEN = "that handle is taken"


@dataclass(frozen=True)
class LinkState:
    """What the owner reads of their own link: its code, and whether it works."""

    code: str
    active: bool


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
    """The invite ``code`` names: a single-use referral first, then an active link.

    A retired code is in neither place, and a revoked link's code is filtered
    here, so both resolve exactly as a code that never existed did. A miss
    always runs both statements. A code no code could be (``is_plausible_code``)
    resolves to nothing without the database, for claim and lookup alike.

    **A link code matches as a handle.** Every link code (default or picked) is
    a lowercase handle, so `Ada-Smith` (typed, or capitalised by a phone) finds
    `ada-smith`. The plausibility check comes first because ``lower()`` folds
    some non-ASCII letters into ASCII ones: the Kelvin sign would become `k`.
    """
    if not is_plausible_code(code):
        return None
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
            .where(
                InviteLink.code == normalise_handle(code),
                # Revoked: resolves for nobody until its owner reactivates it.
                InviteLink.revoked_at.is_(None),
            )
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

    ``None`` for anything that cannot be joined through: unknown, retired,
    revoked, a single-use code somebody already claimed, or an owner who was
    deleted. One answer for all of them, so the public cannot tell them apart.
    """
    resolved = await resolve_code(session, code)
    if resolved is None or not resolved.owner_live:
        return None
    if resolved.referral_id is not None and resolved.invitee_user_id is not None:
        return None
    return resolved


async def _lock_code(session: AsyncSession, code: str) -> None:
    """Serialise every write that takes ``code``, until the transaction ends.

    Two writers taking one free code both pass :func:`_code_taken`, and the
    second would then fail on `uq_invite_links_code`. For a pick (an
    ``UPDATE``) that is a 500; locked, the second runs its check after the first
    commits and is refused with a 409. A default code takes the same lock, so a
    pick of a string a new link has just drawn waits for it too.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:ns, hashtext(:key))"),
        {"ns": INVITE_CODE_LOCK_NAMESPACE, "key": code},
    )


async def _code_taken(session: AsyncSession, code: str, *, link_id: UUID | None) -> bool:
    """Whether ``code`` may not become this link's. ``link_id`` is ``None`` for a
    link not yet created.

    **A retirement in flight cannot slip past this.** The owner's rename changes
    their row and records the retirement in one transaction, so until it commits
    the code is still their current code (taken), and afterwards it is held
    (taken): one statement sees one or the other, never neither.
    """
    return bool(
        await session.scalar(
            select(
                exists().where(InviteLink.code == code)
                | exists().where(Referral.code == code)
                | exists().where(
                    InviteLinkRetiredCode.code == code,
                    # The owner may take back their own; nobody else may.
                    *([InviteLinkRetiredCode.invite_link_id != link_id] if link_id else []),
                    InviteLinkRetiredCode.created_at > func.now() - RETIRED_CODE_HOLD,
                )
            )
        )
    )


async def _own_link(session: AsyncSession, user_id: UUID) -> LinkState | None:
    row = (
        await session.execute(
            select(InviteLink.code, InviteLink.revoked_at).where(InviteLink.user_id == user_id)
        )
    ).one_or_none()
    return None if row is None else LinkState(code=row.code, active=row.revoked_at is None)


async def ensure_invite_link(session: AsyncSession, user_id: UUID) -> tuple[LinkState, bool]:
    """The caller's link, creating it first if needed: ``(state, created)``.

    Read first, since the link usually exists. On a miss, try default codes
    (``{first-name}-{suffix}``) in the order ``DEFAULT_CODE_SUFFIXES`` gives,
    each locked and checked by :func:`_code_taken`, and **let the insert
    arbitrate**: ``DO NOTHING`` on either unique constraint hands back no row.
    Then either a concurrent first request created this user's link (read and
    return it, so both agree) or the code was taken in the meantime (try the
    next). Running out answers ``409``, never ``500``; at five characters of
    suffix that needs millions of links under one first name.
    """
    existing = await _own_link(session, user_id)
    if existing is not None:
        return existing, False

    first_name = await session.scalar(select(User.first_name).where(User.id == user_id))
    for suffix_length in DEFAULT_CODE_SUFFIXES:
        code = default_code(first_name, suffix_length=suffix_length)
        await _lock_code(session, code)
        if await _code_taken(session, code, link_id=None):
            continue
        created = (
            await session.execute(
                pg_insert(InviteLink)
                .values(user_id=user_id, code=code)
                .on_conflict_do_nothing()
                .returning(InviteLink.code)
            )
        ).scalar_one_or_none()
        if created is not None:
            return LinkState(code=created, active=True), True
        winner = await _own_link(session, user_id)
        if winner is not None:
            return winner, False
    raise ConflictError("no free invite code could be chosen; try again")


async def pick_handle(session: AsyncSession, user_id: UUID, handle: str) -> LinkState:
    """Point the caller's link at ``handle``, retiring the code it carried.

    ``handle`` is already normalised and well formed (the domain's rule, checked
    at the boundary). Raises :class:`ConflictError` when :func:`_code_taken`
    says so. Picking the handle you already carry changes nothing. **A rename
    keeps the link's state**: a revoked link stays revoked under its new code.

    **The lock on the handle** (:func:`_lock_code`) makes two picks of one free
    handle a 409 rather than a unique violation. A stranger picking a handle its
    owner is moving off is refused by :func:`_code_taken` with or without it.
    """
    await _lock_code(session, handle)
    if await _own_link(session, user_id) is None:
        # **No link yet: create it with the handle.** Drawing a default code
        # only to retire it would hold, for a month, a code nobody ever saw.
        if await _code_taken(session, handle, link_id=None):
            raise ConflictError(HANDLE_TAKEN)
        created = (
            await session.execute(
                pg_insert(InviteLink)
                .values(user_id=user_id, code=handle)
                .on_conflict_do_nothing(index_elements=["user_id"])
                .returning(InviteLink.code)
            )
        ).scalar_one_or_none()
        if created is not None:
            return LinkState(code=created, active=True)
        # A concurrent first request created the link meanwhile: rename it.
    link = (
        await session.execute(
            select(InviteLink.id, InviteLink.code, InviteLink.revoked_at)
            .where(InviteLink.user_id == user_id)
            .with_for_update()
        )
    ).one()
    state = LinkState(code=handle, active=link.revoked_at is None)
    if link.code == handle:
        return state

    if await _code_taken(session, handle, link_id=link.id):
        raise ConflictError(HANDLE_TAKEN)

    await session.execute(update(InviteLink).where(InviteLink.id == link.id).values(code=handle))
    await session.execute(
        pg_insert(InviteLinkRetiredCode).values(invite_link_id=link.id, code=link.code)
    )
    return state


async def set_link_active(session: AsyncSession, user_id: UUID, *, active: bool) -> LinkState:
    """Revoke or reactivate the caller's link, creating it first if needed.

    Idempotent both ways: revoking a revoked link keeps when it was first
    revoked, and reactivating an active one changes nothing. The code is
    untouched either way, so reactivating makes the same code work again.
    """
    await ensure_invite_link(session, user_id)
    row = (
        await session.execute(
            update(InviteLink)
            .where(InviteLink.user_id == user_id)
            .values(revoked_at=None if active else func.coalesce(InviteLink.revoked_at, func.now()))
            .returning(InviteLink.code, InviteLink.revoked_at)
        )
    ).one()
    return LinkState(code=row.code, active=row.revoked_at is None)
