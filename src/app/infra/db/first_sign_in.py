"""A person's first sign-in creates their account (settled decision #178).

Before this, accounts existed only for migrated users, provisioned ahead of
cutover (decision #40). Anyone new could sign in with Supabase and then receive
a 404 from every endpoint, forever. Now the first authenticated request that
finds no row creates one from the token.

WHAT IT TRUSTS
==============
The token's `sub` and `email`, and nothing else. The token carries no
verification flag, so "this address is the signer's" rests on Supabase Auth's
**Confirm email** setting being on — the hosted default — which gives no session
to an unconfirmed address. Email OTP and Google verify by their nature. That
dependency is recorded in #178 because nothing here can check it.

WHAT IT NEVER DOES
==================
**Link or merge by email.** If the address belongs to any live account this
sign-in is not already linked to — a migrated user provisioning missed, or
someone else's — the answer is `AccountExistsError`, and a person decides.
Linking by email is exactly how an account is taken over.

**Resurrect a deleted account.** A soft-deleted user keeps their `auth_id`
(unique across deleted rows too), so the insert conflicts and nothing is made.

RACE SAFETY
===========
`ON CONFLICT DO NOTHING` against the unique `auth_id` and the unique live email.
Two first requests: one inserts, the other conflicts, finds the row by `auth_id`,
and carries on.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AccountExistsError
from app.infra.db.models.user import User

__all__ = ["provision_first_sign_in"]

logger = logging.getLogger(__name__)


async def provision_first_sign_in(session: AsyncSession, *, auth_id: UUID, email: str) -> None:
    """Create the account for a sign-in that has none, or refuse.

    Returns once a row for `auth_id` exists — created here, or by a request
    that raced this one. Raises `AccountExistsError` when the address belongs
    to an account this sign-in is not linked to. Commits what it creates.
    """
    address = email.strip().lower()
    created = await session.execute(
        insert(User)
        .values(auth_id=auth_id, email=address, email_verified_at=func.now())
        .on_conflict_do_nothing()
        .returning(User.id)
    )
    user_id = created.scalar_one_or_none()
    if user_id is not None:
        await session.commit()
        # The id, never the address: an email in a log line is PII in a place
        # nobody governs.
        logger.info("account created on first sign-in", extra={"user_id": str(user_id)})
        return
    await session.rollback()

    # Nothing inserted, so a unique constraint said no. If it was this
    # sign-in's own `auth_id` — a racing request, or a deleted account — there
    # is nothing to refuse; the caller reads whatever is there.
    linked = await session.execute(select(User.id).where(User.auth_id == auth_id))
    if linked.first() is not None:
        return
    raise AccountExistsError(
        "An account with this email already exists. Contact support to sign in to it."
    )
