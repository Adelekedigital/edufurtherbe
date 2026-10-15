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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.domain.credits import CreditLadder
from app.domain.enums import CreditSource
from app.domain.referrals import NO_INVITE, make_code, may_qualify
from app.infra.db.credit_writer import grant
from app.infra.db.invite_links import ResolvedCode, resolve_code
from app.infra.db.models.referrals import Referral, ReferralUnlock
from app.infra.db.models.user import UserOnboarding

__all__ = ["claim_referral", "create_referral", "qualify_invitee"]

OWN_INVITE = "you cannot claim your own invite"
ALREADY_CLAIMED = "you have already claimed an invite"

#: What a claim returns: the claimant's view of the referral, never the
#: referrer's (`ReferralClaimRead`).
CLAIMED = (Referral.id, Referral.code, Referral.invited_at, Referral.signed_up_at)


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

    The code is either a single-use referral code or a personal link's current
    code; :func:`resolve_code` decides which, in one documented order, for this
    and the public landing lookup alike.

    Idempotent: claiming a code you already hold returns it unchanged rather
    than refusing, because the front end holds the code across signup and a
    retried request must not read as an error.

    **Self-referral is refused here as well as by the database.** The
    ``no_self_referral`` CHECK is the guarantee; this is what makes it a 409
    instead of a 500. The cheapest possible farm is one address referring
    itself, and the gate it opens is recurring.
    """
    referral = await resolve_code(session, code)

    # A deleted referrer or link owner is not found, as on the landing lookup.
    if referral is None or not referral.owner_live:
        raise NotFoundError(NO_INVITE)
    if referral.referral_id is None:
        return await _claim_through_link(session, invitee_id, referral, ladder=ladder)
    if referral.owner_id == invitee_id:
        raise ConflictError(OWN_INVITE)
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
        raise ConflictError(ALREADY_CLAIMED)

    # The check above reads before it writes, so a second claim by the same
    # person racing this one passes it too, and the unique index refuses the
    # loser's update. Inside a savepoint that refusal is the same 409, not a 500.
    try:
        async with session.begin_nested():
            claimed = (
                await session.execute(
                    update(Referral)
                    .where(Referral.id == referral.referral_id, Referral.invitee_user_id.is_(None))
                    .values(invitee_user_id=invitee_id, signed_up_at=func.now())
                    .returning(*CLAIMED)
                )
            ).one_or_none()
    except IntegrityError as error:
        if "uq_referrals_invitee" not in str(error.orig):
            raise
        raise ConflictError(ALREADY_CLAIMED) from None

    if claimed is not None:
        await _qualify_if_finished(session, invitee_id, ladder=ladder)
        return claimed

    # Nothing matched: somebody claimed it between the read and the update.
    # Still yours only if that somebody was you, retrying.
    current = (
        await session.execute(
            select(Referral.invitee_user_id, *CLAIMED).where(Referral.id == referral.referral_id)
        )
    ).one()
    if current.invitee_user_id != invitee_id:
        raise ConflictError("that invite has already been claimed")
    return current


async def _claim_through_link(
    session: AsyncSession, invitee_id: UUID, link: ResolvedCode, *, ladder: CreditLadder
) -> Any:
    """Make this arrival its own referral of the link's owner (decision 254).

    A personal link is reusable, so a claim through it never touches another
    arrival's row: it writes a new `referrals` row for the owner, with a fresh
    single-use code of its own, and every referral rule then applies to it
    unchanged — one invite per arrival, no self-referral, qualification at
    onboarding, and the once-only unlock.

    **The insert arbitrates, against `uq_referrals_invitee`.** Two claims by the
    same arrival racing each other both find nothing held and both insert; the
    loser's ``DO NOTHING`` reads the winner's row, which is the idempotent
    answer when it names the same owner and the 409 when it does not.
    """
    if link.owner_id == invitee_id:
        raise ConflictError(OWN_INVITE)

    held = await _held_by(session, invitee_id)
    if held is None:
        created = (
            await session.execute(
                pg_insert(Referral)
                .values(
                    referrer_id=link.owner_id,
                    code=make_code(),
                    invitee_user_id=invitee_id,
                    signed_up_at=func.now(),
                )
                .on_conflict_do_nothing(
                    index_elements=["invitee_user_id"],
                    index_where=Referral.invitee_user_id.is_not(None),
                )
                .returning(*CLAIMED)
            )
        ).one_or_none()
        if created is not None:
            await _qualify_if_finished(session, invitee_id, ladder=ladder)
            return created
        held = await _held_by(session, invitee_id)

    if held is None or held.referrer_id != link.owner_id:
        raise ConflictError(ALREADY_CLAIMED)
    return held


async def _held_by(session: AsyncSession, invitee_id: UUID) -> Any:
    """The referral this person already arrived through, if any."""
    return (
        await session.execute(
            select(Referral.referrer_id, *CLAIMED).where(Referral.invitee_user_id == invitee_id)
        )
    ).one_or_none()


async def _qualify_if_finished(
    session: AsyncSession, invitee_id: UUID, *, ladder: CreditLadder
) -> None:
    """**Qualify now if they have already finished.**

    Qualification normally fires from `complete_onboarding`, which assumes the
    claim came first. It often will not: the natural first run is to sign in,
    complete the profile the app puts in front of you, and paste the invite code
    afterwards. In that order the claim set `signed_up_at` and nothing ever
    revisited the row — `qualified_at` stayed null forever and the referrer was
    never paid, with no second completion coming to fix it. Found by code review.

    Same transaction as the claim, for the same reason completion holds its own:
    the two halves commit together or neither does.
    """
    if await session.scalar(
        select(UserOnboarding.completed_at).where(
            UserOnboarding.user_id == invitee_id,
            UserOnboarding.completed_at.is_not(None),
        )
    ):
        await qualify_invitee(session, invitee_id, ladder=ladder)


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
