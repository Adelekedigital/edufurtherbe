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
from sqlalchemy.orm import aliased

from app.infra.db.models.education import DegreeLevel, EducationEntry, Institution
from app.infra.db.models.mentoring import (
    MenteeGoal,
    MenteeGoalCountry,
    MenteeGoalNeed,
    MentorProfile,
    ServiceOffering,
)
from app.infra.db.models.reference import Country, Language
from app.infra.db.models.scholarships import ScholarshipProgram, UserAward
from app.infra.db.models.sessions import SessionTypeBookingConfig
from app.infra.db.models.user import UserLanguage
from app.infra.db.offerings import offerings_for

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
            EducationEntry.degree_abbreviation,
            DegreeLevel.slug.label("degree_level_slug"),
            DegreeLevel.display_name.label("degree_level_name"),
            DegreeLevel.short_name.label("degree_level_short_name"),
            *_INSTITUTION_REF,
        )
        # Outer joins throughout: an entry with no matched institution and no
        # mapped degree level is the ordinary migrated case, not an error, and an
        # inner join would silently drop exactly those rows.
        .outerjoin(Institution, Institution.id == EducationEntry.institution_id)
        .outerjoin(Country, Country.id == Institution.country_id)
        .outerjoin(DegreeLevel, DegreeLevel.id == EducationEntry.degree_level_id)
        .where(EducationEntry.user_id == user_id, EducationEntry.deleted_at.is_(None))
        # **`is_most_recent` is deliberately not an ordering key**, though it was
        # one until now. It is blank on all 21 dev-export rows (D98), so sorting
        # by it was a no-op that read as a rule — and would have silently
        # reordered every list the day anybody set it. `date_start` breaks the tie
        # between two entries that ended in the same year, and `id` makes the
        # order total so a page cannot shuffle between requests.
        .order_by(
            EducationEntry.date_end.desc().nulls_last(),
            EducationEntry.date_start.desc().nulls_last(),
            EducationEntry.id.desc(),
        )
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

    **``default_meeting_venue`` and ``requires_booking_confirmation`` come from
    the primary offering** (D88), not from ``mentor_profiles``, and both are null
    when there is no primary. That is not a degraded read: after the move these
    settings only exist on an offering, so a mentor who has claimed none has no
    value rather than a default one. Reporting ``'google_meet'`` and ``false``
    there would invent a booking policy for someone who cannot be booked.
    """
    primary = aliased(SessionTypeBookingConfig, name="primary_config")
    row = (
        (
            await session.execute(
                select(
                    MentorProfile.id,
                    MentorProfile.headline,
                    MentorProfile.years_of_experience,
                    MentorProfile.approval_status,
                    MentorProfile.listing_status,
                    primary.requires_booking_confirmation,
                    primary.meeting_venue.label("default_meeting_venue"),
                    MentorProfile.primary_study_program,
                    Country.code.label("primary_study_country_code"),
                    Country.display_name.label("primary_study_country_name"),
                )
                .outerjoin(Country, Country.id == MentorProfile.primary_study_country_id)
                # Outer, because a mentor need not have a primary offering — the
                # guard on retiring one makes releasing the pointer a legitimate
                # intermediate state, and a new mentor has never had one.
                .outerjoin(
                    primary, primary.session_type_id == MentorProfile.primary_session_type_id
                )
                .where(MentorProfile.user_id == user_id, MentorProfile.deleted_at.is_(None))
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None

    # Shared with the public profile rather than repeated there. See
    # `infra/db/offerings.py` for why the filter lives inside it.
    # One id, because the shared query is batched for discovery. Absent from
    # the mapping means "this mentor claimed none", which is an empty list here.
    grouped = await offerings_for(session, [user_id])
    return dict(row) | {"offerings": grouped.get(user_id, [])}


async def list_languages(session: AsyncSession, user_id: UUID) -> list[dict[str, Any]]:
    """The languages a user speaks, alphabetically.

    **Not "primary first", though the column invites it.** `user_languages` has
    `is_primary`, and the ETL writes `false` on every migrated row — it is
    derived from a Bubble column that carries no such distinction (D27). Ordering
    by a field that is uniformly one value is a rule that does nothing and reads
    as though it does, which is the same shape as `is_most_recent` on education
    and `verification_status` on awards. Alphabetical is honest and total.

    `proficiency` is deliberately absent from the projection. The column is
    `NOT NULL` with a `'fluent'` default and the ETL never sets it, so every
    migrated row claims fluency nobody was asked about. It becomes returnable
    when a write path collects it.
    """
    statement = (
        select(
            Language.id,
            Language.display_name,
            Language.code_639_3.label("code"),
        )
        .join(UserLanguage, UserLanguage.language_id == Language.id)
        .where(UserLanguage.user_id == user_id)
        .order_by(Language.display_name)
    )
    return [dict(row) for row in (await session.execute(statement)).mappings()]
