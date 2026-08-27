"""Marking onboarding finished, and paying the credit that goes with it.

**This is the producer `profile_completed` did not have.** Before it,
`user_onboarding.completed_at` was written by exactly one thing — the ETL — so
the starter credit had nothing to fire it and the earning model's first rung was
unreachable from the API.

ONE TRANSACTION, TWO FACTS
==========================
The completion and the grant are written together and committed by the caller.
Split across two transactions there is a state where a user is marked complete
and holds no credit, and nothing would ever revisit it: completion is recorded,
so a retry is a no-op, and the credit is simply missing forever.

Idempotence comes from two different mechanisms, deliberately. The completion is
idempotent because it keeps the *first* timestamp — re-finishing does not move
the date somebody finished. The grant is idempotent because of a partial unique
index. Neither reads-then-writes.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import OnboardingIncompleteError
from app.domain.credits import CreditLadder
from app.domain.notifications import Notification
from app.domain.onboarding import ProfileEvidence, may_complete_onboarding
from app.infra.db.credit_writer import grant_starter
from app.infra.db.models.mentoring import MenteeGoal, MentorProfile
from app.infra.db.models.user import UserOnboarding, UserProfile
from app.infra.db.outbox import enqueue
from app.infra.db.referral_writer import qualify_invitee

__all__ = ["OnboardingResult", "complete_onboarding"]


@dataclass(frozen=True, slots=True)
class OnboardingResult:
    """What happened, so the route can say 201 or 200 honestly.

    A frozen dataclass, matching `AuthUser` and `ProfileEvidence` rather than a
    hand-rolled ``__slots__`` class.
    """

    completed_at: dt.datetime
    last_step: str | None
    #: True only on the call that actually created the lot. A retry reports
    #: False, which is how the route distinguishes 201 from 200 without
    #: counting rows.
    granted: bool


async def _evidence(session: AsyncSession, user_id: UUID) -> ProfileEvidence:
    """Three existence checks in one round trip.

    ``EXISTS`` rather than fetching the rows: nothing here needs a column, and
    selecting one would invite somebody to start reading it and quietly widen
    the predicate the domain rule owns.
    """
    row = (
        await session.execute(
            select(
                exists().where(UserProfile.user_id == user_id).label("profile"),
                exists().where(MenteeGoal.user_id == user_id).label("goal"),
                exists().where(MentorProfile.user_id == user_id).label("mentor"),
            )
        )
    ).one()

    return ProfileEvidence(
        has_profile=bool(row.profile),
        has_mentee_goal=bool(row.goal),
        has_mentor_profile=bool(row.mentor),
    )


async def complete_onboarding(
    session: AsyncSession, user_id: UUID, *, ladder: CreditLadder
) -> OnboardingResult:
    """Mark onboarding finished and grant the starter. Does not commit.

    Raises :class:`OnboardingIncompleteError` when the profile does not meet the
    bar — a refusal the caller must answer by sending the user back to finish,
    never by retrying, which is why it carries its own problem type.
    """
    if not may_complete_onboarding(await _evidence(session, user_id)):
        raise OnboardingIncompleteError("Finish your profile before completing onboarding.")

    # **Keeps the first timestamp.** `COALESCE` on the existing value rather
    # than `EXCLUDED`, so re-finishing does not move the date somebody actually
    # finished — which the starter's "never expires" makes harmless today and a
    # dated grant would make wrong.
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

    granted = await grant_starter(session, user_id, ladder=ladder) is not None

    if granted:
        # **In the granting transaction**, so a message about a credit that
        # rolled back cannot exist — the same rule `write_review` follows for
        # the mentor's notice.
        #
        # Guarded on `granted` rather than sent unconditionally: a retried
        # completion creates no lot, and telling somebody twice that their first
        # credit arrived is worse than a slightly late first telling.
        await enqueue(
            session,
            Notification.CREDITS_GRANTED,
            entity_type="user",
            entity_id=user_id,
            recipient_ids=(user_id,),
        )

    # **The referrer's half, in this transaction.** Finishing a profile is what
    # qualifies the invite that named this user — the same signal as the starter
    # credit, and the target settled decision 20 always pointed at.
    #
    # Not a separate call after the commit: that leaves a state where the
    # invitee is complete, so a retry is a no-op, and the referrer's credits are
    # missing with nothing that would ever revisit it.
    await qualify_invitee(session, user_id, ladder=ladder)

    return OnboardingResult(completed_at=row.completed_at, last_step=row.last_step, granted=granted)
