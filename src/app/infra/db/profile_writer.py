"""Writing a user's goals, awards, mentor profile and profile.

Mirrors ``profile_store``, which reads the same four subjects — one module per
side rather than four small modules per side, so a field added to a read and
forgotten on the write is visible in two files instead of eight. Education is
separate because it carries the institution transaction, which is a different
concern rather than a bigger one.

**Every statement is scoped by `user_id` in its own `WHERE`**, not only by the
dependency that resolved the URL. The dependency guards the URL; the statement
guards the row.

DELETE MEANS TWO DIFFERENT THINGS HERE
======================================
``user_awards`` carries ``deleted_at`` and ``mentee_goals`` does not, so
deleting an award hides it and deleting a goal destroys it. That asymmetry is
the schema's, not a choice made here, and it is stated in both route
descriptions — a caller cannot infer from `DELETE` which one they are getting.

THE GOAL SATELLITES BELONG TO THE USER, NOT THE GOAL
====================================================
``mentee_goal_countries`` and ``mentee_goal_needs`` key on ``user_id``. So
submitting countries with a goal replaces *the user's* countries, and a user
with two goals sees the same list on both. Writing them per goal would claim a
relationship the tables do not carry.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.db.models.mentoring import (
    MenteeGoal,
    MenteeGoalCountry,
    MenteeGoalNeed,
    MentorProfile,
    MentorServiceOffering,
)
from app.infra.db.models.scholarships import UserAward
from app.infra.db.models.user import User, UserLanguage, UserProfile

GOAL_COLUMNS = ("degree_goal_id", "degree_goal_raw", "target_start_term", "notes")
AWARD_COLUMNS = ("title", "institution", "scholarship_program_id", "year", "evidence_url")
#: `requires_booking_confirmation` is back, and `_fan_out_booking_confirmation`
#: is gone with it. The fan-out existed only because the column had moved to
#: `session_type_booking_configs` while this endpoint was still the mentor's only
#: control over it — so a toggle had to be copied onto every live offering or it
#: would answer 200 and change nothing anybody read. With the mentor column
#: authoritative again the toggle writes one row, and two writers for one column
#: is the duplication non-negotiable #8 calls a defect.
MENTOR_COLUMNS = (
    "headline",
    "years_of_experience",
    "requires_booking_confirmation",
    "primary_study_country_id",
    "primary_study_program",
)
PROFILE_COLUMNS = (
    "about_me",
    "gender",
    "origin_country_id",
    "social_linkedin",
    "social_twitter",
    "social_youtube",
)


def _sent(payload: dict[str, Any], columns: tuple[str, ...]) -> dict[str, Any]:
    """Only the keys the client actually sent.

    **Taking `payload.get(key)` for every column is wrong on an insert**: it
    writes an explicit `NULL` where the client said nothing, and an explicit NULL
    overrides a server default rather than falling back to it.

    The worked example is `mentor_profiles.requires_booking_confirmation` —
    `NOT NULL DEFAULT false`, so applying to be a mentor without mentioning it
    raises a not-null violation, caught by a test rather than by reading. It left
    this table under D88 and has now come back, so the example is live again
    rather than historical. The trap outlived its absence either way: it caught
    the test factory in the reader step, where naming `meeting_venue`
    unconditionally sent a NULL that a fresh server default could not rescue.
    """
    return {key: value for key, value in payload.items() if key in columns}


def _rowcount(result: Any) -> int:
    """``AsyncSession.execute`` is typed as ``Result``, which has no rowcount."""
    return cast("CursorResult[Any]", result).rowcount


# --------------------------------------------------------------------------
# Goals
# --------------------------------------------------------------------------


async def upsert_goal(session: AsyncSession, user_id: UUID, payload: dict[str, Any]) -> UUID:
    """Create or replace the user's goal. There is only ever one.

    ``mentee_goals.user_id`` is ``unique=True`` and the model states "1:1 with
    the user", so appending violates the constraint on the second call — which
    is how this was found, by a test creating two goals.

    It is also why the countries and needs satellites key on ``user_id``: with
    one goal per user, user-keyed and goal-keyed are the same thing, and the
    asymmetry I flagged in review does not exist.
    """
    existing = await session.execute(select(MenteeGoal.id).where(MenteeGoal.user_id == user_id))
    row = existing.first()
    values = {key: value for key, value in payload.items() if key in GOAL_COLUMNS}

    if row is None:
        created = await session.execute(
            insert(MenteeGoal)
            .values(user_id=user_id, **_sent(payload, GOAL_COLUMNS))
            .returning(MenteeGoal.id)
        )
        goal_id: UUID = created.scalar_one()
    else:
        goal_id = row[0]
        if values:
            await session.execute(
                update(MenteeGoal).where(MenteeGoal.user_id == user_id).values(**values)
            )

    await _write_goal_satellites(session, user_id, payload)
    return goal_id


async def _write_goal_satellites(
    session: AsyncSession, user_id: UUID, payload: dict[str, Any]
) -> None:
    """Only when the key was sent. Absent means "leave alone"; an empty list
    means "remove them all", and conflating the two makes a request that omits
    countries silently wipe them."""
    if payload.get("country_ids") is not None:
        ids = payload["country_ids"]
        await session.execute(delete(MenteeGoalCountry).where(MenteeGoalCountry.user_id == user_id))
        if ids:
            await session.execute(
                insert(MenteeGoalCountry),
                [
                    {"user_id": user_id, "country_id": value, "priority": index + 1}
                    for index, value in enumerate(ids)
                ],
            )
    if payload.get("need_ids") is not None:
        ids = payload["need_ids"]
        await session.execute(delete(MenteeGoalNeed).where(MenteeGoalNeed.user_id == user_id))
        if ids:
            await session.execute(
                insert(MenteeGoalNeed),
                [{"user_id": user_id, "service_offering_id": value} for value in ids],
            )


async def delete_goal(session: AsyncSession, user_id: UUID) -> bool:
    """A **real** delete: `mentee_goals` has no `deleted_at`.

    The satellites are left alone deliberately — they belong to the user, and
    clearing a goal is not a statement about which countries they are aiming
    for.
    """
    result = await session.execute(delete(MenteeGoal).where(MenteeGoal.user_id == user_id))
    return _rowcount(result) > 0


# --------------------------------------------------------------------------
# Awards
# --------------------------------------------------------------------------


async def create_award(session: AsyncSession, user_id: UUID, payload: dict[str, Any]) -> UUID:
    """`verification_status` is left to its default — self-reported, and nothing
    verifies one yet. A caller setting it would be verifying themselves."""
    result = await session.execute(
        insert(UserAward)
        .values(user_id=user_id, **_sent(payload, AWARD_COLUMNS))
        .returning(UserAward.id)
    )
    return result.scalar_one()


async def update_award(
    session: AsyncSession, user_id: UUID, award_id: UUID, payload: dict[str, Any]
) -> bool:
    values = {key: value for key, value in payload.items() if key in AWARD_COLUMNS}
    if not values:
        found = await session.execute(
            select(UserAward.id).where(
                UserAward.id == award_id,
                UserAward.user_id == user_id,
                UserAward.deleted_at.is_(None),
            )
        )
        return found.first() is not None
    result = await session.execute(
        update(UserAward)
        .where(
            UserAward.id == award_id,
            UserAward.user_id == user_id,
            UserAward.deleted_at.is_(None),
        )
        .values(**values)
    )
    return _rowcount(result) > 0


async def delete_award(session: AsyncSession, user_id: UUID, award_id: UUID) -> bool:
    """Soft — `user_awards` carries `deleted_at`, unlike `mentee_goals`."""
    result = await session.execute(
        update(UserAward)
        .where(
            UserAward.id == award_id,
            UserAward.user_id == user_id,
            UserAward.deleted_at.is_(None),
        )
        .values(deleted_at=func.now())
    )
    return _rowcount(result) > 0


# --------------------------------------------------------------------------
# Mentor profile
# --------------------------------------------------------------------------


async def create_mentor_profile(
    session: AsyncSession, user_id: UUID, payload: dict[str, Any]
) -> UUID:
    """Apply to be a mentor.

    `approval_status` is left to its `'pending'` server default: the applicant
    does not decide whether they are approved.

    **`primary_role` flips to mentor in the same transaction**, because that is
    what decides which dashboard they land on next, and a new mentor sent to the
    mentee dashboard reads as the application having failed. It grants nothing —
    `primary_role` is a dashboard hint and never an authorization claim
    (settled decision 17); what a pending mentor may *do* is gated on
    `approval_status`.
    """
    result = await session.execute(
        insert(MentorProfile)
        .values(user_id=user_id, **_sent(payload, MENTOR_COLUMNS))
        .returning(MentorProfile.id)
    )
    await session.execute(update(User).where(User.id == user_id).values(primary_role="mentor"))
    await _write_offerings(session, user_id, payload)
    return result.scalar_one()


async def _write_offerings(session: AsyncSession, user_id: UUID, payload: dict[str, Any]) -> None:
    if "offering_ids" not in payload or payload["offering_ids"] is None:
        return
    ids = payload["offering_ids"]
    await session.execute(
        delete(MentorServiceOffering).where(MentorServiceOffering.mentor_user_id == user_id)
    )
    if ids:
        await session.execute(
            insert(MentorServiceOffering),
            [{"mentor_user_id": user_id, "service_offering_id": value} for value in ids],
        )


async def update_mentor_profile(
    session: AsyncSession, user_id: UUID, payload: dict[str, Any]
) -> bool:
    """`approval_status`, `listing_status` and every audit column are absent from
    `MENTOR_COLUMNS`, so a mentor cannot approve or relist themselves."""
    found = await session.execute(
        select(MentorProfile.id).where(
            MentorProfile.user_id == user_id, MentorProfile.deleted_at.is_(None)
        )
    )
    if found.first() is None:
        return False
    values = {key: value for key, value in payload.items() if key in MENTOR_COLUMNS}
    if values:
        await session.execute(
            update(MentorProfile)
            .where(MentorProfile.user_id == user_id, MentorProfile.deleted_at.is_(None))
            .values(**values)
        )
    await _write_offerings(session, user_id, payload)
    return True


# --------------------------------------------------------------------------
# The profile itself
# --------------------------------------------------------------------------


async def upsert_profile(session: AsyncSession, user_id: UUID, payload: dict[str, Any]) -> None:
    """Write the profile, creating the row if there is not one.

    **Upsert rather than update**: `/me` already reports `has_profile: false`,
    so a user editing their bio for the first time has no row to update and a
    plain `UPDATE` would silently write nothing and answer 200.

    `avatar_url` and `banner_url` are not in `PROFILE_COLUMNS`. Images are
    content-addressed in Supabase Storage (ADR 0019); accepting a URL here would
    let a profile point at any host and bypass that scheme.
    """
    values = {key: value for key, value in payload.items() if key in PROFILE_COLUMNS}
    existing = await session.execute(
        select(UserProfile.user_id).where(UserProfile.user_id == user_id)
    )
    if existing.first() is None:
        await session.execute(insert(UserProfile).values(user_id=user_id, **values))
        return
    if values:
        await session.execute(
            update(UserProfile).where(UserProfile.user_id == user_id).values(**values)
        )


# --------------------------------------------------------------------------
# Languages
# --------------------------------------------------------------------------


async def replace_languages(
    session: AsyncSession, user_id: UUID, entries: list[dict[str, Any]]
) -> None:
    """Replace the user's languages wholesale.

    **Delete then insert, in that order and in one transaction.** Two unique
    partial indexes make anything else fail:
    `ix_user_languages_one_primary` allows one primary per user, and
    `ix_user_languages_user_language` allows a language once — so writing the new
    primary before clearing the old one raises, exactly as `is_most_recent` does
    on education. Clearing first makes both constraints a backstop rather than
    the thing the user meets.

    Replace rather than merge, because the request carries the whole list and a
    merge could not express removal: a user unticking their last language would
    have no way to say so.
    """
    await session.execute(delete(UserLanguage).where(UserLanguage.user_id == user_id))
    if not entries:
        return
    await session.execute(
        insert(UserLanguage),
        [
            {
                "user_id": user_id,
                "language_id": entry["language_id"],
                "proficiency": entry["proficiency"],
                "is_primary": entry["is_primary"],
            }
            for entry in entries
        ],
    )
