"""Reading a user's own onboarding record.

Its own module beside `onboarding_writer.py` for the reason every other pair in
this package is split: a read is safe to call from anywhere and a write is not,
and putting them in one file makes that distinction a matter of remembering.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BookingNeedsOnboardingError
from app.domain.onboarding import ProfileEvidence, missing_codes
from app.infra.db.models.mentoring import MenteeGoal, MentorProfile
from app.infra.db.models.user import UserOnboarding, UserProfile
from app.infra.db.offerings import live_goal_exists

__all__ = [
    "get_onboarding",
    "onboarding_evidence",
    "onboarding_state",
    "require_finished_onboarding",
]


async def get_onboarding(session: AsyncSession, user_id: UUID) -> Any | None:
    """This user's onboarding row, or ``None`` if they have never started.

    Null rather than a synthesised empty record: a user who has not begun is a
    different fact from one who began and got nowhere, and `last_step` cannot
    tell them apart because it is null in both.
    """
    return (
        await session.execute(
            select(UserOnboarding.completed_at, UserOnboarding.last_step).where(
                UserOnboarding.user_id == user_id
            )
        )
    ).one_or_none()


async def onboarding_evidence(session: AsyncSession, user_id: UUID) -> ProfileEvidence:
    """Every fact the completion rule reads, in one round trip."""
    evidence, _, _ = await onboarding_state(session, user_id)
    return evidence


async def onboarding_state(
    session: AsyncSession, user_id: UUID
) -> tuple[ProfileEvidence, bool, str | None]:
    """The completion rule's facts and whether onboarding is finished, in one
    round trip.

    ``EXISTS`` rather than fetching the rows: nothing here needs a value, and
    selecting one would invite somebody to start reading it and quietly widen
    the predicate the domain rule owns. Every check names the caller. Read by
    completion and by `/me`, so the two cannot disagree (decision 261).
    """
    row = (
        await session.execute(
            select(
                exists()
                .where(UserOnboarding.user_id == user_id, UserOnboarding.completed_at.is_not(None))
                .label("completed"),
                select(UserOnboarding.last_step)
                .where(UserOnboarding.user_id == user_id)
                .scalar_subquery()
                .label("last_step"),
                exists().where(UserProfile.user_id == user_id).label("profile"),
                exists().where(MenteeGoal.user_id == user_id).label("goal"),
                exists().where(MentorProfile.user_id == user_id).label("mentor"),
                live_goal_exists(user_id).label("need"),
                exists()
                .where(UserProfile.user_id == user_id, UserProfile.origin_country_id.is_not(None))
                .label("origin"),
                exists()
                .where(MenteeGoal.user_id == user_id, MenteeGoal.degree_goal_id.is_not(None))
                .label("degree"),
                exists()
                .where(MenteeGoal.user_id == user_id, MenteeGoal.field_of_study.is_not(None))
                .label("field"),
            )
        )
    ).one()

    evidence = ProfileEvidence(
        has_profile=bool(row.profile),
        has_mentee_goal=bool(row.goal),
        has_mentor_profile=bool(row.mentor),
        has_live_need=bool(row.need),
        has_origin=bool(row.origin),
        has_degree_goal=bool(row.degree),
        has_field_of_study=bool(row.field),
    )
    return evidence, bool(row.completed), row.last_step


async def require_finished_onboarding(session: AsyncSession, user_id: UUID) -> None:
    """Refuse a caller who has not finished onboarding, naming what is owed.

    **Keyed on ``completed_at``**, not on ``missing`` (owner, 2026-10-18): a
    migrated mentee carries Bubble's completion date and counts as finished
    even where today's bar names answers they never gave (decision 261).
    """
    finished = exists().where(
        UserOnboarding.user_id == user_id, UserOnboarding.completed_at.is_not(None)
    )
    if (await session.execute(select(finished))).scalar():
        return
    # Only a refusal pays for the full evidence, which names what is owed.
    evidence, _, _ = await onboarding_state(session, user_id)
    raise BookingNeedsOnboardingError(
        "Finish setting up your account before booking.", missing=missing_codes(evidence)
    )
