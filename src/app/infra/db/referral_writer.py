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
**A signup carries no invite code.** ``users`` rows are created at first
sign-in (decision 178), by ``provisioning_store`` (the CLI) and by the ETL, and
none of them knows which invite brought the person. So the invitee claims their
code once authenticated, and that is the moment recorded. The account's own
``created_at`` is what decides whether it may still claim (decision 264).

The column name is the package's. It is kept because a later Supabase webhook
would fill exactly this column with exactly this meaning — but the gap between
the name and what the service actually witnessed is real, and worth knowing
before somebody reasons about signup funnels from it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Collection
from typing import Any
from uuid import UUID

from sqlalchemy import exists, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, InviteForNewAccountsError, NotFoundError
from app.domain.credits import CreditLadder
from app.domain.enums import CreditSource
from app.domain.referrals import NO_INVITE, make_code, may_qualify
from app.infra.db.credit_writer import grant
from app.infra.db.invite_links import ResolvedCode, resolve_code
from app.infra.db.models.referrals import Referral, ReferralUnlock
from app.infra.db.models.sessions import Session
from app.infra.db.models.user import User, UserOnboarding
from app.infra.db.session_stats import attended_by

__all__ = ["claim_referral", "create_referral", "qualify_invitee", "unlock_referrers"]

OWN_INVITE = "you cannot claim your own invite"
ALREADY_CLAIMED = "you have already claimed an invite"
CLAIMED_BY_ANOTHER = "that invite has already been claimed"
NOT_NEW = "invites are for new accounts"

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
    session: AsyncSession,
    invitee_id: UUID,
    code: str,
    *,
    ladder: CreditLadder,
    window: dt.timedelta,
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

    **Only a new account attaches** (decision 264). The age check is the last
    one before a write, so every other answer is unchanged for an old account:
    an unknown code is still `404`, the `409`s still say what they said, and an
    invite the caller already holds is still returned.
    """
    referral = await resolve_code(session, code)

    # A deleted referrer or link owner is not found, as on the landing lookup.
    if referral is None or not referral.owner_live:
        raise NotFoundError(NO_INVITE)
    if referral.referral_id is None:
        return await _claim_through_link(
            session, invitee_id, referral, ladder=ladder, window=window
        )
    if referral.owner_id == invitee_id:
        raise ConflictError(OWN_INVITE)
    if referral.invitee_user_id is not None and referral.invitee_user_id != invitee_id:
        raise ConflictError(CLAIMED_BY_ANOTHER)
    if referral.invitee_user_id == invitee_id:
        # A retry: returned before the age check, so it never reads as an error.
        return (
            await session.execute(select(*CLAIMED).where(Referral.id == referral.referral_id))
        ).one()

    # **One invite per arrival.** `uq_referrals_invitee` is the guarantee; this
    # is what makes it a 409 the caller can act on rather than a 500 they
    # cannot. Claiming a *second, different* code is refused — which matters
    # because until this existed, doing so locked the user out of onboarding
    # permanently.
    if await session.scalar(select(Referral.id).where(Referral.invitee_user_id == invitee_id)):
        raise ConflictError(ALREADY_CLAIMED)
    await _refuse_an_old_account(session, invitee_id, window)

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
        raise ConflictError(CLAIMED_BY_ANOTHER)
    return current


async def _claim_through_link(
    session: AsyncSession,
    invitee_id: UUID,
    link: ResolvedCode,
    *,
    ladder: CreditLadder,
    window: dt.timedelta,
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
        await _refuse_an_old_account(session, invitee_id, window)
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


async def _refuse_an_old_account(
    session: AsyncSession, invitee_id: UUID, window: dt.timedelta
) -> None:
    """Refuse a caller whose account is older than ``window`` (decision 264).

    Compared with the database's ``now()`` in the claim's transaction, never
    the process clock, so the boundary is one clock's. At exactly
    ``window`` the account is still new. A missing row reads as old, so
    the check fails closed.
    """
    is_new = await session.scalar(
        select(User.created_at >= func.now() - window).where(User.id == invitee_id)
    )
    if not is_new:
        raise InviteForNewAccountsError(NOT_NEW)


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
    """This user finished onboarding: qualify their invite, pay the referrer's
    bonus, and unlock the referrer if they have attended a session.

    Returns whether an unlock was created. Does not commit; the caller owns the
    transaction, and here that caller is ``complete_onboarding``.

    **Two facts since decision 295.** The bonus (`ladder.unlock`) is paid for the
    referrer's first qualified invite, as before. The unlock, which is what the
    monthly grant reads, also waits for the referrer to attend a session as a
    mentee: whichever comes second writes it (:func:`unlock_referrers`).

    **The bonus is once-ever, under the referrer's row lock.** Paid only when no
    other referral of theirs is qualified and no unlock exists, so a second
    invitee pays nothing and a grandfathered user (an unlock with no referral)
    is not paid, exactly as when the unlock row was the bonus's gate. Two
    invitees finishing at once queue on the lock, and the second sees the first.

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

    await _lock_referrers(session, [referral.referrer_id])
    first = not await session.scalar(
        select(
            exists().where(
                Referral.referrer_id == referral.referrer_id,
                Referral.qualified_at.is_not(None),
            )
            | exists().where(ReferralUnlock.user_id == referral.referrer_id)
        )
    )
    await session.execute(
        update(Referral).where(Referral.id == referral.id).values(qualified_at=func.now())
    )
    if first:
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
    return bool(await unlock_referrers(session, [referral.referrer_id]))


async def unlock_referrers(session: AsyncSession, user_ids: Collection[UUID]) -> int:
    """Unlock each of ``user_ids`` who has a qualified invite **and** has
    attended a session as a mentee (decision 295). Returns how many were
    unlocked now. Does not commit.

    Called from both halves, because either can come second: qualifying an
    invite (:func:`qualify_invitee`) and settling sessions
    (`settle_attendance`, for the mentees who came).

    **Locked first, then read.** Each half reads the other's fact, so without
    the referrer's row lock a qualification and a settlement committing
    together would each miss the other and neither would write. Once-ever and
    never revoked: ``ON CONFLICT DO NOTHING`` on ``uq_referral_unlocks_user_id``,
    crediting the earliest qualified invite.
    """
    if not user_ids:
        return 0
    await _lock_referrers(session, user_ids)
    attended = (
        select(Session.id)
        .where(Session.mentee_id == Referral.referrer_id, attended_by(Session.mentee_id))
        .exists()
    )
    first_qualified = (
        select(Referral.referrer_id, Referral.id)
        .distinct(Referral.referrer_id)
        .where(
            Referral.referrer_id.in_(list(user_ids)),
            Referral.qualified_at.is_not(None),
            attended,
        )
        .order_by(Referral.referrer_id, Referral.qualified_at, Referral.id)
    )
    unlocked = await session.execute(
        pg_insert(ReferralUnlock)
        .from_select(["user_id", "unlocked_by_referral_id"], first_qualified)
        .on_conflict_do_nothing(index_elements=["user_id"])
        .returning(ReferralUnlock.id)
    )
    return len(unlocked.all())


async def _lock_referrers(session: AsyncSession, user_ids: Collection[UUID]) -> None:
    """The referrers' `users` rows, locked in id order so two writers locking
    overlapping sets cannot deadlock on each other."""
    await session.execute(
        select(User.id).where(User.id.in_(list(user_ids))).order_by(User.id).with_for_update()
    )
