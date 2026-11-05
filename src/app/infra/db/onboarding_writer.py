"""Marking onboarding finished and paying its credit, and saving the last step.

**Finishing onboarding is level 1** (decision 307): the completion tops the
mentee's month up to it. Before decision 307 this paid a once-ever starter lot.

ONE TRANSACTION, TWO FACTS
==========================
The completion and the grant are written together and committed by the caller.
Split across two transactions there is a state where a user is marked complete
and holds no credit, and nothing would ever revisit it: completion is recorded,
so a retry is a no-op, and the credit is simply missing forever.

Idempotence: the completion keeps the *first* timestamp, so re-finishing does
not move the date somebody finished, and the top-up pays only what the period
still owes their level, so a retry pays nothing. Both run under the user's row
lock, which is also what makes "the first completion" a fact two concurrent
calls cannot both see.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import OnboardingIncompleteError
from app.domain.credits import CreditLadder
from app.domain.notifications import Notification
from app.domain.onboarding import (
    finishes_as_mentee,
    may_complete_onboarding,
    missing_codes,
)
from app.infra.db.credit_grants import top_up_levels
from app.infra.db.models.referrals import Referral
from app.infra.db.models.user import UserOnboarding
from app.infra.db.onboarding_store import onboarding_state
from app.infra.db.outbox import enqueue
from app.infra.db.referral_writer import qualify_invitee
from app.infra.db.user_locks import lock_users

__all__ = ["OnboardingResult", "complete_onboarding", "record_last_step"]


@dataclass(frozen=True, slots=True)
class OnboardingResult:
    """What happened, so the route can say 201 or 200 honestly.

    A frozen dataclass, matching `AuthUser` and `ProfileEvidence` rather than a
    hand-rolled ``__slots__`` class.
    """

    completed_at: dt.datetime
    last_step: str | None
    #: True only on the call that actually paid credits. A retry reports False,
    #: which is how the route distinguishes 201 from 200 without counting rows.
    granted: bool
    #: True only on a mentee's first completion (`completed_at` was unset): the
    #: one that queues the bio draft (decision 269).
    drafts_bio: bool = False


async def _finished(session: AsyncSession, user_id: UUID) -> Any | None:
    """The caller's onboarding row when it is already finished, else `None`."""
    return (
        await session.execute(
            select(UserOnboarding.completed_at, UserOnboarding.last_step).where(
                UserOnboarding.user_id == user_id, UserOnboarding.completed_at.is_not(None)
            )
        )
    ).one_or_none()


async def complete_onboarding(
    session: AsyncSession, user_id: UUID, *, ladder: CreditLadder
) -> OnboardingResult:
    """Mark onboarding finished and top the month up to the user's level. Does
    not commit.

    Raises :class:`OnboardingIncompleteError` when the profile does not meet the
    bar — a refusal the caller must answer by sending the user back to finish,
    never by retrying, which is why it carries its own problem type.

    **A user already finished is answered `200` without the check, and paid
    nothing**, as the route publishes: the bar can tighten after someone
    finished (every migrated mentee lacks a field of study, decision 261) and a
    retry must not refuse an account that is done. Their level is paid by the
    1st and by the events that raise it; paying here as well would make a retry
    on the 1st, before the job ran, answer `201` and send the first-credits
    email a second time.

    **The invitee and their referrer are locked together, in id order, first.**
    Completion tops up the invitee and then qualifies the referrer; taking the
    two locks one after the other could deadlock with a settlement locking the
    same pair in id order.
    """
    referrer = await session.scalar(
        select(Referral.referrer_id).where(Referral.invitee_user_id == user_id)
    )
    await lock_users(session, [user_id] if referrer is None else [user_id, referrer])

    settled = await _finished(session, user_id)
    if settled is not None:
        return OnboardingResult(
            completed_at=settled.completed_at, last_step=settled.last_step, granted=False
        )
    evidence, _, _ = await onboarding_state(session, user_id)
    if not may_complete_onboarding(evidence):
        raise OnboardingIncompleteError(
            "Finish your profile before completing onboarding.",
            missing=missing_codes(evidence),
        )

    # **Keeps the first timestamp.** `COALESCE` on the existing value rather
    # than `EXCLUDED`, so re-finishing does not move the date somebody actually
    # finished.
    row = (
        await session.execute(
            pg_insert(UserOnboarding)
            .values(user_id=user_id, completed_at=func.now())
            .on_conflict_do_update(
                index_elements=["user_id"],
                set_={"completed_at": func.coalesce(UserOnboarding.completed_at, func.now())},
            )
            .returning(UserOnboarding.completed_at, UserOnboarding.last_step)
        )
    ).one()

    # Level 1 now, or higher if an invite of theirs already qualified. App
    # clock, matching `credit_store`'s read.
    paid = await top_up_levels(session, [user_id], ladder=ladder, now=dt.datetime.now(dt.UTC))
    granted = bool(paid)

    if granted:
        # **In the granting transaction**, so a message about a credit that
        # rolled back cannot exist — the same rule `write_review` follows for
        # the mentor's notice.
        #
        # Guarded on `granted` rather than sent unconditionally: a retried
        # completion pays nothing, and telling somebody twice that their first
        # credit arrived is worse than a slightly late first telling.
        await enqueue(
            session,
            Notification.CREDITS_GRANTED,
            entity_type="user",
            entity_id=user_id,
            recipient_ids=(user_id,),
        )

    # **The referrer's half, in this transaction.** Finishing a profile is what
    # qualifies the invite that named this user — the same signal as their own
    # level 1, and the target settled decision 20 always pointed at.
    #
    # Not a separate call after the commit: that leaves a state where the
    # invitee is complete, so a retry is a no-op, and the referrer's credits are
    # missing with nothing that would ever revisit it.
    await qualify_invitee(session, user_id, ladder=ladder)

    return OnboardingResult(
        completed_at=row.completed_at,
        last_step=row.last_step,
        granted=granted,
        # Only the first completion reaches here (a finished user returned
        # above, read under the lock), so two concurrent calls cannot both
        # queue a draft.
        drafts_bio=finishes_as_mentee(evidence),
    )


async def record_last_step(session: AsyncSession, user_id: UUID, step: str) -> Any:
    """Save where the caller got to, creating their row if needed. Does not commit.

    One statement, so two first saves racing both land on the unique
    ``user_id`` rather than one failing. **Only `last_step` is in the update**:
    `completed_at` is the completion endpoint's, and a step saved after
    finishing must neither clear nor move it. The step is checked against
    `STEPS` at the boundary (decision 260). The race is pinned by
    `test_two_first_saves_racing_both_land`.
    """
    statement = pg_insert(UserOnboarding).values(user_id=user_id, last_step=step)
    return (
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=["user_id"],
                set_={"last_step": statement.excluded.last_step},
            ).returning(UserOnboarding.completed_at, UserOnboarding.last_step)
        )
    ).one()
