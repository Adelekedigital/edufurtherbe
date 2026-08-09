"""Reading a user's own attributes: education, goals, awards, mentor profile.

**Every statement here is already scoped to one user before it runs.** The caller
passes a `user_id` that `api.deps.get_target_user` has resolved — a row it could
not find is a caller who may not read it — so nothing in this module decides
authorization, and nothing in this module may be called with an unchecked id.

SOFT DELETES
============
``education_entries``, ``user_awards`` and ``mentor_profiles`` all carry
``deleted_at``; ``mentee_goals`` does not. Every read of the first three excludes
deleted rows, and `test_profile_store_soft_deletes` derives which tables need
that from the metadata rather than from a list somebody maintains — because
this rule has now been missed twice in this repository, once on `users` across
five statements and once here on `user_awards`.

THE INSTITUTION JOIN CARRIES NO STATUS FILTER, DELIBERATELY
===========================================================
``catalogue_store.VISIBLE`` excludes `pending_review` and merged rows from
*search*, so nobody selects an unvetted duplicate. Copying that predicate into
this join would blank the school on the profile of the very person who created
it — the one user for whom that row is real and who is waiting on the review.
Search decides what may be *offered*; reading an entity follows the foreign key.

That is the single most copy-able mistake in this PR, which is why it is written
here rather than left to be inferred from the absence of a `WHERE` clause.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.db.models.education import DegreeLevel, EducationEntry, Institution
from app.infra.db.models.mentoring import (
    MenteeGoal,
    MenteeGoalCountry,
    MenteeGoalNeed,
    MentorProfile,
    MentorServiceOffering,
    ServiceOffering,
)
from app.infra.db.models.reference import Country
from app.infra.db.models.scholarships import ScholarshipProgram, UserAward

#: The institution columns an embedded reference carries, matching what
#: `catalogue_store` projects — so `InstitutionRead.from_row` builds the same
#: shape whether the row came from search or from an education entry.
_INSTITUTION_REF = (
    Institution.id.label("institution_id"),
    Institution.name.label("institution_name"),
    Institution.web_page.label("institution_web_page"),
    Country.code.label("country_code"),
    Country.display_name.label("country_name"),
)


def _education_statement(user_id: UUID) -> Select[Any]:
    return (
        select(
            EducationEntry.id,
            EducationEntry.school_name_raw,
            EducationEntry.degree_category,
            EducationEntry.study_course,
            EducationEntry.study_program,
            EducationEntry.date_start,
            EducationEntry.date_end,
            EducationEntry.is_most_recent,
            DegreeLevel.slug.label("degree_level_slug"),
            DegreeLevel.display_name.label("degree_level_name"),
            *_INSTITUTION_REF,
        )
        # Outer joins throughout: an entry with no matched institution and no
        # mapped degree level is the ordinary migrated case, not an error, and an
        # inner join would silently drop exactly those rows.
        .outerjoin(Institution, Institution.id == EducationEntry.institution_id)
        .outerjoin(Country, Country.id == Institution.country_id)
        .outerjoin(DegreeLevel, DegreeLevel.id == EducationEntry.degree_level_id)
        .where(EducationEntry.user_id == user_id, EducationEntry.deleted_at.is_(None))
        .order_by(EducationEntry.is_most_recent.desc(), EducationEntry.date_end.desc().nulls_last())
    )


async def list_education(session: AsyncSession, user_id: UUID) -> list[dict[str, Any]]:
    """One user's degrees, most recent first."""
    return [dict(row) for row in (await session.execute(_education_statement(user_id))).mappings()]


async def get_goal(session: AsyncSession, user_id: UUID) -> dict[str, Any] | None:
    """The user's goal, or ``None``. There is at most one.

    ``mentee_goals.user_id`` is ``unique=True`` and the model states "1:1 with
    the user". This returned a list until a write test hit the constraint —
    a list that could only ever hold zero or one, telling every client that
    goals are a collection.

    Countries and needs are fetched per goal rather than as one join, because a
    goal with three countries and four needs would otherwise come back as twelve
    rows to be de-duplicated in Python — a fan-out that is wrong slightly more
    often than it is written.
    """
    goals = (
        await session.execute(
            select(
                MenteeGoal.id,
                MenteeGoal.degree_goal_raw,
                MenteeGoal.target_start_term,
                MenteeGoal.notes,
                DegreeLevel.slug.label("degree_goal_slug"),
                DegreeLevel.display_name.label("degree_goal_name"),
            )
            .outerjoin(DegreeLevel, DegreeLevel.id == MenteeGoal.degree_goal_id)
            .where(MenteeGoal.user_id == user_id)
            .order_by(MenteeGoal.created_at)
        )
    ).mappings()

    countries = (
        await session.execute(
            select(Country.code, Country.display_name, MenteeGoalCountry.priority)
            # `select_from` is not decoration here: the projection names
            # `Country`, so without it SQLAlchemy infers `Country` as the FROM
            # and then cannot join it to itself.
            .select_from(MenteeGoalCountry)
            .join(Country, Country.id == MenteeGoalCountry.country_id)
            .where(MenteeGoalCountry.user_id == user_id)
            .order_by(MenteeGoalCountry.priority)
        )
    ).mappings()

    needs = (
        await session.execute(
            select(ServiceOffering.slug, ServiceOffering.display_name)
            .select_from(MenteeGoalNeed)
            .join(ServiceOffering, ServiceOffering.id == MenteeGoalNeed.service_offering_id)
            .where(MenteeGoalNeed.user_id == user_id)
            .order_by(ServiceOffering.sort_order)
        )
    ).mappings()

    # Both satellites hang off `user_id`, not off a goal id — the legacy shape,
    # kept deliberately (settled decision on the satellite tables). So they are
    # the same lists for every goal this user has, and attaching them per goal
    # would claim a per-goal relationship the data does not carry.
    shared_countries = [dict(row) for row in countries]
    shared_needs = [dict(row) for row in needs]
    first = next(iter(goals), None)
    if first is None:
        return None
    return dict(first) | {"countries": shared_countries, "needs": shared_needs}


async def list_awards(session: AsyncSession, user_id: UUID) -> list[dict[str, Any]]:
    """One user's scholarships and awards, newest first."""
    statement = (
        select(
            UserAward.id,
            UserAward.institution,
            UserAward.title,
            UserAward.year,
            UserAward.verification_status,
            UserAward.evidence_url,
            ScholarshipProgram.display_name.label("programme_name"),
        )
        .outerjoin(ScholarshipProgram, ScholarshipProgram.id == UserAward.scholarship_program_id)
        # `deleted_at IS NULL` is not optional here, and it was missed once.
        # `ix_user_awards_user` is declared `WHERE deleted_at IS NULL`, so a
        # query without the predicate cannot use it — the index exists for this
        # statement and this statement could not reach it.
        .where(UserAward.user_id == user_id, UserAward.deleted_at.is_(None))
        .order_by(UserAward.year.desc().nulls_last(), UserAward.title)
    )
    return [dict(row) for row in (await session.execute(statement)).mappings()]


async def get_mentor_profile(session: AsyncSession, user_id: UUID) -> dict[str, Any] | None:
    """A user's mentor profile, or ``None`` if they are not a mentor.

    ``None`` rather than an empty object: most users have no mentor profile, and
    an empty object would say "they are a mentor with nothing filled in", which
    is a different and wrong claim.
    """
    row = (
        (
            await session.execute(
                select(
                    MentorProfile.id,
                    MentorProfile.headline,
                    MentorProfile.years_of_experience,
                    MentorProfile.approval_status,
                    MentorProfile.listing_status,
                    MentorProfile.requires_booking_confirmation,
                    MentorProfile.default_meeting_venue,
                    MentorProfile.primary_study_program,
                    Country.code.label("primary_study_country_code"),
                    Country.display_name.label("primary_study_country_name"),
                )
                .outerjoin(Country, Country.id == MentorProfile.primary_study_country_id)
                .where(MentorProfile.user_id == user_id, MentorProfile.deleted_at.is_(None))
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None

    offerings = (
        await session.execute(
            select(ServiceOffering.slug, ServiceOffering.display_name)
            .select_from(MentorServiceOffering)
            .join(
                ServiceOffering,
                ServiceOffering.id == MentorServiceOffering.service_offering_id,
            )
            .where(MentorServiceOffering.mentor_user_id == user_id)
            .order_by(ServiceOffering.sort_order)
        )
    ).mappings()
    return dict(row) | {"offerings": [dict(o) for o in offerings]}
