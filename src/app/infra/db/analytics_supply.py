"""The admin Analytics Supply tab (PRD 31 PR 3, decision 324): are there
enough mentors, and where are mentees going unserved.

* **cohort**: mentor applications submitted in the window (`submitted_at`),
  profile and account not deleted, judged by where they stand **now**:
  `applied` all of them, `approved` approved, `listed` public
  (`mentor_is_public`), `bookable` taking bookings (`taking_bookings`).
* **median_review_days**: approval and decline decisions made in the window,
  the median of decision time minus the profile's `submitted_at`, in days. A
  profile never submitted has no review time and is skipped.
* **unlisted**: `unlisted` events in the window, except the side effects
  `SIDE_EFFECT_UNLISTINGS` (a decline, a deleted account).
* **by_country** / **by_school**: now, not windowed. Live mentees aiming for a
  goal country, or a visible catalogue school, against mentors taking bookings
  who guide that country, or have a live education entry at that school.

Everyone `analytics_scope` leaves out is left out of every figure.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Float, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.analytics import (
    SIDE_EFFECT_UNLISTINGS,
    SupplyCohort,
    SupplyPlace,
    SupplyTab,
    days_of,
    neediest,
)
from app.domain.enums import MentorStatusType
from app.domain.mentor_stats import Window
from app.infra.db.analytics_scope import counted_user
from app.infra.db.catalogue_store import VISIBLE
from app.infra.db.models.education import EducationEntry, Institution
from app.infra.db.models.mentoring import (
    MenteeGoal,
    MenteeGoalCountry,
    MenteeGoalSchool,
    MentorGuideCountry,
    MentorProfile,
    MentorStatusEvent,
)
from app.infra.db.models.reference import Country
from app.infra.db.models.user import User
from app.infra.db.predicates import LIVE, during, mentor_approved
from app.infra.db.public_visibility import (
    bookable_mentors,
    mentor_exists,
    mentor_is_public,
    taking_bookings,
)

#: The decisions an application waits for.
_DECISIONS = (MentorStatusType.APPROVED, MentorStatusType.DECLINED)


async def supply_tab(session: AsyncSession, window: Window) -> SupplyTab:
    """The tab's figures: the cohort, review time and unlistings for
    ``window``; the places as they stand now."""
    return SupplyTab(
        cohort=await _cohort(session, window),
        median_review_days=await median_review_days(session, window),
        unlisted=int(await session.scalar(_unlistings(window)) or 0),
        by_country=neediest(await _places(session, _countries())),
        by_school=neediest(await _places(session, _schools())),
    )


async def _cohort(session: AsyncSession, window: Window) -> SupplyCohort:
    def counting(*clauses: Any) -> Any:
        return func.count().filter(*clauses)

    row = (
        await session.execute(
            select(
                func.count(),
                counting(mentor_approved()),
                counting(*mentor_is_public()),
                counting(taking_bookings()),
            )
            .select_from(MentorProfile)
            .join(User, User.id == MentorProfile.user_id)
            .where(
                during(MentorProfile.submitted_at, window),
                *mentor_exists(),
                counted_user(MentorProfile.user_id),
            )
        )
    ).one()
    applied, approved, listed, bookable = (int(n) for n in row)
    return SupplyCohort(applied=applied, approved=approved, listed=listed, bookable=bookable)


async def median_review_days(session: AsyncSession, window: Window) -> float | None:
    """``median_review_days`` for ``window``: the Supply tab's, and the figure
    beside the mentor application funnel (decision 338)."""
    return days_of(await session.scalar(_median_review_seconds(window)))


def _median_review_seconds(window: Window) -> Any:
    """`percentile_cont` skips the nulls a never-submitted profile gives."""
    waited = func.extract("epoch", MentorStatusEvent.created_at - MentorProfile.submitted_at)
    return (
        select(func.percentile_cont(0.5).within_group(cast(waited, Float)))
        .select_from(MentorStatusEvent)
        .join(MentorProfile, MentorProfile.user_id == MentorStatusEvent.mentor_user_id)
        .where(
            MentorStatusEvent.status_type.in_(_DECISIONS),
            during(MentorStatusEvent.created_at, window),
            counted_user(MentorStatusEvent.mentor_user_id),
        )
    )


def _unlistings(window: Window) -> Any:
    """`reason` is free text and often null; null is not a side effect."""
    return select(func.count()).where(
        MentorStatusEvent.status_type == MentorStatusType.UNLISTED,
        or_(
            MentorStatusEvent.reason.is_(None),
            MentorStatusEvent.reason.not_in(SIDE_EFFECT_UNLISTINGS),
        ),
        during(MentorStatusEvent.created_at, window),
        counted_user(MentorStatusEvent.mentor_user_id),
    )


async def _places(session: AsyncSession, parts: tuple[Any, Any, Any]) -> list[SupplyPlace]:
    """Every place with a mentee, beside its bookable mentors (0 for none)."""
    places, mentees, mentors = parts
    rows = await session.execute(
        select(places.c.key, places.c.name, mentees.c.n, func.coalesce(mentors.c.n, 0))
        .select_from(places)
        .join(mentees, mentees.c.place == places.c.id)
        .outerjoin(mentors, mentors.c.place == places.c.id)
    )
    return [
        SupplyPlace(key=str(key), name=name, mentees=int(n), bookable_mentors=int(m))
        for key, name, n, m in rows.all()
    ]


def _taking_bookings(mentor_id: Any) -> list[Any]:
    """``mentor_id`` is a counted mentor taking bookings now."""
    return [mentor_id.in_(bookable_mentors()), counted_user(mentor_id)]


def _countries() -> tuple[Any, Any, Any]:
    """Goal countries, and the bookable mentors who guide each."""
    places = select(
        Country.id.label("id"), Country.code.label("key"), Country.display_name.label("name")
    ).subquery()
    mentees = (
        select(
            MenteeGoalCountry.country_id.label("place"),
            func.count(func.distinct(MenteeGoalCountry.user_id)).label("n"),
        )
        .join(User, User.id == MenteeGoalCountry.user_id)
        .where(LIVE, counted_user(MenteeGoalCountry.user_id))
        .group_by(MenteeGoalCountry.country_id)
        .subquery()
    )
    mentors = (
        select(
            MentorGuideCountry.country_id.label("place"),
            func.count(func.distinct(MentorGuideCountry.mentor_user_id)).label("n"),
        )
        .where(*_taking_bookings(MentorGuideCountry.mentor_user_id))
        .group_by(MentorGuideCountry.country_id)
        .subquery()
    )
    return places, mentees, mentors


def _schools() -> tuple[Any, Any, Any]:
    """Visible catalogue goal schools, and the bookable mentors who studied
    at each (a live education entry there)."""
    places = (
        select(
            Institution.id.label("id"),
            Institution.id.label("key"),
            Institution.name.label("name"),
        )
        .where(VISIBLE)
        .subquery()
    )
    mentees = (
        select(
            MenteeGoalSchool.institution_id.label("place"),
            func.count(func.distinct(MenteeGoal.user_id)).label("n"),
        )
        .join(MenteeGoal, MenteeGoal.id == MenteeGoalSchool.goal_id)
        .join(User, User.id == MenteeGoal.user_id)
        .where(LIVE, counted_user(MenteeGoal.user_id))
        .group_by(MenteeGoalSchool.institution_id)
        .subquery()
    )
    mentors = (
        select(
            EducationEntry.institution_id.label("place"),
            func.count(func.distinct(EducationEntry.user_id)).label("n"),
        )
        .where(EducationEntry.deleted_at.is_(None), *_taking_bookings(EducationEntry.user_id))
        .group_by(EducationEntry.institution_id)
        .subquery()
    )
    return places, mentees, mentors
