"""Submitting a mentor application, and what it is checked against.

Creating a mentor profile is saving step one of onboarding, not applying
(Mentor Onboarding PRD, B2; decision 270): admins are alerted only when the
mentor submits a complete application, once. Completeness is `domain.mentor_application`; this
module gathers its evidence in one statement and makes the transition.

**Every statement names the caller**: there is no path here that reads or
writes another mentor's row.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    MentorApplicationDecidedError,
    MentorApplicationIncompleteError,
    NotFoundError,
)
from app.domain.enums import ApprovalStatus
from app.domain.mentor_application import ApplicationEvidence, missing_items
from app.domain.notifications import Notification
from app.infra.db.admin_store import admins_who_can_decide
from app.infra.db.mentor_status_store import lock_mentor
from app.infra.db.models.education import EducationEntry
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.user import UserProfile
from app.infra.db.offerings import claims_a_live_help_area
from app.infra.db.outbox import enqueue
from app.infra.db.predicates import blank
from app.infra.db.public_visibility import has_live_offering, has_weekly_hours

__all__ = ["SubmittedApplication", "application_evidence", "submit_application"]


@dataclass(frozen=True, slots=True)
class SubmittedApplication:
    submitted_at: datetime
    approval_status: str


def _filled(column: Any) -> Any:
    return ~blank(column)


def _has_education() -> Any:
    return exists().where(
        EducationEntry.user_id == MentorProfile.user_id,
        EducationEntry.deleted_at.is_(None),
        EducationEntry.degree_level_id.is_not(None),
        _filled(EducationEntry.study_course),
        EducationEntry.date_start.is_not(None),
        EducationEntry.date_end.is_not(None),
    )


async def application_evidence(
    session: AsyncSession, user_id: UUID
) -> tuple[ApplicationEvidence, str, datetime | None] | None:
    """What this mentor's application holds, its approval status and when it
    was submitted; ``None`` when they have no live mentor profile."""
    row = (
        (
            await session.execute(
                select(
                    claims_a_live_help_area(MentorProfile.user_id).label("has_offering"),
                    _has_education().label("has_education"),
                    MentorProfile.primary_study_country_id.is_not(None).label(
                        "has_primary_study_country"
                    ),
                    # Never null on the outer join: `IS NOT NULL` and `blank` are
                    # both false-or-true for a missing profile row.
                    _filled(UserProfile.avatar_url).label("has_avatar"),
                    UserProfile.origin_country_id.is_not(None).label("has_origin_country"),
                    _filled(UserProfile.social_linkedin).label("has_linkedin"),
                    _filled(MentorProfile.headline).label("has_headline"),
                    has_weekly_hours().label("has_availability"),
                    has_live_offering().label("has_session_type"),
                    MentorProfile.approval_status,
                    MentorProfile.submitted_at,
                )
                .outerjoin(UserProfile, UserProfile.user_id == MentorProfile.user_id)
                .where(MentorProfile.user_id == user_id, MentorProfile.deleted_at.is_(None))
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    evidence = ApplicationEvidence(
        **{f.name: bool(row[f.name]) for f in fields(ApplicationEvidence)}
    )
    return evidence, str(row["approval_status"]), row["submitted_at"]


async def submit_application(session: AsyncSession, user_id: UUID) -> SubmittedApplication:
    """Submit the caller's application and tell the admins, once. Does not commit.

    **Order**: no profile is a 404; a decided application is a 409 whatever it
    holds, because there is nothing left to submit; an application already
    submitted answers 200 unchanged, so a retry sends nothing; only then is
    completeness checked. The mentor row is locked first (`lock_mentor`, the
    lock every status transition takes), so two submits and a decision run one
    after the other and exactly one submit sends the alert.
    """
    if not await lock_mentor(session, user_id):
        raise NotFoundError("you have no mentor profile")
    state = await application_evidence(session, user_id)
    if state is None:  # pragma: no cover - the lock found the same row
        raise NotFoundError("you have no mentor profile")
    evidence, approval, submitted_at = state

    if approval != ApprovalStatus.PENDING.value:
        raise MentorApplicationDecidedError(f"your application was already {approval}")
    if submitted_at is not None:
        return SubmittedApplication(submitted_at=submitted_at, approval_status=approval)
    missing = missing_items(evidence)
    if missing:
        raise MentorApplicationIncompleteError(
            "Finish your application before submitting it.",
            missing=tuple(item.value for item in missing),
        )

    submitted = (
        await session.execute(
            update(MentorProfile)
            .where(MentorProfile.user_id == user_id, MentorProfile.submitted_at.is_(None))
            .values(submitted_at=func.now())
            .returning(MentorProfile.submitted_at)
        )
    ).scalar_one()
    await _alert_reviewers(session, user_id)
    # Just set to `now()`, so never null.
    return SubmittedApplication(submitted_at=cast(datetime, submitted), approval_status=approval)


async def _alert_reviewers(session: AsyncSession, user_id: UUID) -> None:
    """Tell the admins who can act, in the submit's transaction.

    Without it an application is found by somebody thinking to look. **Not the
    applicant, even if they are an admin**: being told to review your own
    application is noise, though the decision endpoint permits deciding it.
    """
    reviewers = tuple(admin for admin in await admins_who_can_decide(session) if admin != user_id)
    if reviewers:
        await enqueue(
            session,
            Notification.MENTOR_APPLICATION_RECEIVED,
            entity_type="mentor_profile",
            entity_id=user_id,
            recipient_ids=reviewers,
        )
