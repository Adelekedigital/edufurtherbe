"""Creating invites, claiming them, and the unlock one of them opens.

Three writes, and the third is the one that costs money.

NOTHING HERE COMMITS
====================
The caller does. :func:`qualify_invitee` in particular runs inside the
transaction that completes onboarding, and that is the whole point: the
invitee's completion, the referral's qualification, and the referrer's grant are
one fact or none. Split across transactions there is a state where the invitee
is complete, the referral is qualified, and the referrer's two credits are
simply missing — with nothing that would ever revisit it, because the completion
is recorded and a retry is a no-op.

``signed_up_at`` IS SET AT CLAIM TIME, NOT AT SIGNUP
====================================================
**This service cannot observe a signup.** ``users`` rows are created by
``provisioning_store`` (the CLI) and by the ETL; there is no registration
endpoint here to notice an arrival. So the invitee claims their code once
authenticated, and that is the moment recorded.

The column name is the package's. It is kept because a later Supabase webhook
would fill exactly this column with exactly this meaning — but the gap between
the name and what the service actually witnessed is real, and worth knowing
before somebody reasons about signup funnels from it.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.domain.credits import CreditLadder
from app.domain.enums import CreditSource
from app.domain.referrals import make_code, may_qualify
from app.infra.db.credit_writer import grant
from app.infra.db.models.referrals import Referral, ReferralUnlock
from app.infra.db.models.user import UserOnboarding

__all__ = ["claim_referral", "create_referral", "qualify_invitee"]


async def create_referral(
    session: AsyncSession, referrer_id: UUID, invitee_email: str | None
) -> Any:
    """Record an invite and return it. Does not commit.

    A duplicate address for the same referrer is refused by
    ``uq_referrals_referrer_id_invitee`` and surfaces as a
    :class:`ConflictError` — translating it here rather than letting an
    ``IntegrityError`` reach the error handler, because a 500 is the wrong way
    to learn you already invited somebody.
    """
    # **The insert arbitrates, not a prior read.** A read-then-insert loses the
    # race a double-clicked submit button creates: both requests see nothing,
    # both insert, and the second violates `uq_referrals_referrer_id_invitee` —
    # which is not in `STATUS_BY_ERROR`, so the caller gets a 500 telling them
    # nothing. `ON CONFLICT DO NOTHING` reports the loser as no row, and the
    # 409 below is the answer the docstring always promised.
    created = (
        await session.execute(
            pg_insert(Referral)
            .values(referrer_id=referrer_id, code=make_code(), invitee_email=invitee_email)
            .on_conflict_do_nothing(index_elements=["referrer_id", "invitee_email"])
            .returning(
                Referral.id,
                Referral.code,
                Referral.invitee_email,
                Referral.invited_at,
                Referral.signed_up_at,
                Referral.qualified_at,
            )
        )
    ).one_or_none()

    if created is None:
        raise ConflictError("you have already invited this address")

    return created


async def claim_referral(
    session: AsyncSession, invitee_id: UUID, code: str, *, ladder: CreditLadder
) -> Any:
    """Attach the caller to the invite that named this code. Does not commit.

    Idempotent: claiming a code you already hold returns it unchanged rather
    than refusing, because the front end holds the code across signup and a
    retried request must not read as an error.

    **Self-referral is refused here as well as by the database.** The
    ``no_self_referral`` CHECK is the guarantee; this is what makes it a 409
    instead of a 500. The cheapest possible farm is one address referring
    itself, and the gate it opens is recurring.
    """
    referral = (
        await session.execute(
            select(Referral.id, Referral.referrer_id, Referral.invitee_user_id).where(
                Referral.code == code
            )
        )
    ).one_or_none()

    if referral is None:
        raise NotFoundError("no invite carries that code")
    if referral.referrer_id == invitee_id:
        raise ConflictError("you cannot claim your own invite")
    if referral.invitee_user_id is not None and referral.invitee_user_id != invitee_id:
        raise ConflictError("that invite has already been claimed")

    # **One invite per arrival.** `uq_referrals_invitee` is the guarantee; this
    # is what makes it a 409 the caller can act on rather than a 500 they
    # cannot. Claiming a *second, different* code is refused — which matters
    # because until this existed, doing so locked the user out of onboarding
    # permanently.
    if referral.invitee_user_id is None and await session.scalar(
        select(Referral.id).where(Referral.invitee_user_id == invitee_id)
    ):
        raise ConflictError("you have already claimed an invite")

    claimed = (
        await session.execute(
            update(Referral)
            .where(Referral.id == referral.id, Referral.invitee_user_id.is_(None))
            .values(invitee_user_id=invitee_id, signed_up_at=func.now())
            .returning(Referral.id, Referral.code, Referral.invited_at, Referral.signed_up_at)
        )
    ).one_or_none()

    if claimed is not None:
        # **Qualify now if they have already finished.**
        #
        # Qualification normally fires from `complete_onboarding`, which assumes
        # the claim came first. It often will not: the natural first run is to
        # sign in, complete the profile the app puts in front of you, and paste
        # the invite code afterwards. In that order the claim set `signed_up_at`
        # and nothing ever revisited the row — `qualified_at` stayed null
        # forever and the referrer was never paid, with no second completion
        # coming to fix it. Found by code review.
        #
        # Same transaction as the claim, for the same reason completion holds
        # its own: the two halves commit together or neither does.
        if await session.scalar(
            select(UserOnboarding.completed_at).where(
                UserOnboarding.user_id == invitee_id,
                UserOnboarding.completed_at.is_not(None),
            )
        ):
            await qualify_invitee(session, invitee_id, ladder=ladder)
        return claimed

    return (
        await session.execute(
            select(Referral.id, Referral.code, Referral.invited_at, Referral.signed_up_at).where(
                Referral.id == referral.id
            )
        )
    ).one()


async def qualify_invitee(session: AsyncSession, invitee_id: UUID, *, ladder: CreditLadder) -> bool:
    """This user finished onboarding — qualify their invite and pay the referrer.

    Returns whether an unlock was created. Does not commit; the caller owns the
    transaction, and here that caller is ``complete_onboarding``.

    **The unlock is once-ever and the database says so.** ``ON CONFLICT DO
    NOTHING`` against ``uq_referral_unlocks_user_id`` rather than reading first:
    a referrer's second qualifying invitee must produce no second unlock and no
    second grant, and a read-then-write is a check-time-of-use race the moment
    two invitees finish at once.

    A referrer who is *already* unlocked still has the referral marked
    qualified — the invite genuinely qualified, and the ledger should say so
    even though it opens a floor that is already open.
    """
    referral = (
        await session.execute(
            select(Referral.id, Referral.referrer_id, Referral.qualified_at).where(
                Referral.invitee_user_id == invitee_id
            )
        )
    ).one_or_none()

    if referral is None:
        return False
    if not may_qualify(has_invitee=True, already_qualified=referral.qualified_at is not None):
        return False

    await session.execute(
        update(Referral).where(Referral.id == referral.id).values(qualified_at=func.now())
    )

    unlock_id = (
        await session.execute(
            pg_insert(ReferralUnlock)
            .values(user_id=referral.referrer_id, unlocked_by_referral_id=referral.id)
            .on_conflict_do_nothing(index_elements=["user_id"])
            .returning(ReferralUnlock.id)
        )
    ).scalar_one_or_none()

    if unlock_id is None:
        return False

    await grant(
        session,
        referral.referrer_id,
        source=CreditSource.REFERRAL_UNLOCK,
        quantity=ladder.unlock,
        # App clock, matching `credit_store`'s read. Mixing the two would put
        # the boundary a lot expires on in a different frame from the filter
        # that decides whether it is spendable.
        now=dt.datetime.now(dt.UTC),
    )
    return True
