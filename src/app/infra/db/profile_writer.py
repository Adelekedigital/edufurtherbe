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

**Schools are the exception, and belong to the goal.** ``mentee_goal_schools``
keys on ``goal_id``, so they are replaced per goal and go when it does. Each is
resolved by ``education_writer.resolve_institution``, the education entry's own
path, so a typed school matches and queues the same way wherever it is typed.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, case, delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationError
from app.domain.enums import LanguageProficiency
from app.infra.db.avatar_focus_store import chosen_focus_columns
from app.infra.db.education_writer import resolve_institution
from app.infra.db.models.mentoring import (
    MenteeGoal,
    MenteeGoalCountry,
    MenteeGoalNeed,
    MenteeGoalSchool,
    MentorProfile,
    MentorServiceOffering,
)
from app.infra.db.models.reference import Country
from app.infra.db.models.scholarships import UserAward
from app.infra.db.models.user import User, UserLanguage, UserProfile

GOAL_COLUMNS = (
    "degree_goal_id",
    "degree_goal_raw",
    "field_of_study",
    "target_start_term",
    "notes",
)
AWARD_COLUMNS = (
    "title",
    "institution",
    "scholarship_program_id",
    "year",
    "evidence_url",
    "funding",
)
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
    "booking_window_days",
    "break_after_minutes",
    "default_duration_minutes",
    "default_min_notice_minutes",
    "primary_study_country_id",
    "primary_study_program",
)
#: Columns of the profile write that live on `users`, not `user_profiles`.
USER_COLUMNS = ("timezone", "first_name", "last_name", "primary_role")

PROFILE_COLUMNS = (
    "about_me",
    "gender",
    "origin_country_id",
    "current_country_id",
    "cover_color",
    "cover_art",
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
    await _write_goal_schools(session, user_id, goal_id, payload)
    return goal_id


async def _write_goal_schools(
    session: AsyncSession, user_id: UUID, goal_id: UUID, payload: dict[str, Any]
) -> None:
    """Replace the goal's schools, in the order sent, when the key was sent.

    Absent or `null` leaves them, as the satellites above; `[]` clears them.
    **The goal row is locked first**, scoped to its owner, so two replacements
    of one goal run one after the other rather than meeting on
    `uq_mentee_goal_schools_goal_position`. Delete-then-insert because the
    request carries the whole list and each school is a new row.
    """
    if payload.get("schools") is None:
        return
    await session.execute(
        select(MenteeGoal.id)
        .where(MenteeGoal.id == goal_id, MenteeGoal.user_id == user_id)
        .with_for_update()
    )
    await session.execute(
        delete(MenteeGoalSchool).where(
            MenteeGoalSchool.goal_id == goal_id,
            MenteeGoalSchool.goal_id.in_(
                select(MenteeGoal.id).where(MenteeGoal.user_id == user_id)
            ),
        )
    )
    rows = []
    for position, school in enumerate(payload["schools"]):
        institution_id, _created = await resolve_institution(
            session,
            school_name_raw=school["school_name_raw"],
            supplied=school.get("institution_id"),
            user_id=user_id,
        )
        rows.append(
            {
                "goal_id": goal_id,
                "institution_id": institution_id,
                "school_name_raw": school["school_name_raw"],
                "position": position,
            }
        )
    if rows:
        await session.execute(insert(MenteeGoalSchool), rows)


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
    """Start a mentor application: save its first step.

    **Not submitting it, and nobody is told.** Admins are alerted when the
    mentor submits a complete application (`mentor_application_store`), not
    when a profile is saved partway through onboarding.

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
    # **The refusals first**, so one raised before anything is written leaves
    # nothing to roll back: an unknown country, then the chosen crop.
    await _refuse_unknown_countries(session, payload)
    if "avatar_focus" in payload:
        await _choose_avatar_focus(session, user_id, payload["avatar_focus"])
    # **`timezone`, the name and `primary_role` live on `users`**, so they are written there,
    # scoped to the owner's id, in this same transaction (booking request item 7).
    user_values = {key: value for key, value in payload.items() if key in USER_COLUMNS}
    if user_values:
        await session.execute(update(User).where(User.id == user_id).values(**user_values))
    values = {key: value for key, value in payload.items() if key in PROFILE_COLUMNS}
    existing = await session.execute(
        select(UserProfile.user_id).where(UserProfile.user_id == user_id)
    )
    found = existing.first() is not None
    values.update(_provenance(payload, stored=found))
    if not found:
        # A request that only set a `users` column, or only said a bio that
        # does not exist is their own, is not starting a profile: creating an
        # empty row would flip `/me`'s `profile` from null.
        if values or not (user_values or "about_me_drafted" in payload):
            await session.execute(insert(UserProfile).values(user_id=user_id, **values))
        return
    if values:
        await session.execute(
            update(UserProfile).where(UserProfile.user_id == user_id).values(**values)
        )


#: The profile's country references, each a `countries.id`.
COUNTRY_FIELDS = ("origin_country_id", "current_country_id")


async def _refuse_unknown_countries(session: AsyncSession, payload: dict[str, Any]) -> None:
    """A `422` at each country field whose id names no country.

    The foreign key would refuse it too, as an `IntegrityError` and a 500; one
    query asked first turns that into an answer naming the field.
    """
    sent = {field: payload[field] for field in COUNTRY_FIELDS if payload.get(field) is not None}
    if not sent:
        return
    known = set(
        (await session.execute(select(Country.id).where(Country.id.in_(sent.values())))).scalars()
    )
    errors = tuple(
        (f"/{field}", "not a country in /catalog/countries")
        for field, value in sent.items()
        if value not in known
    )
    if errors:
        raise ValidationError("unknown country", field_errors=errors)


def _provenance(payload: dict[str, Any], *, stored: bool) -> dict[str, Any]:
    """The bio's provenance columns this write sets (decision 267).

    `about_me_drafted: true` stamps the database's `now()` and the version;
    `false` clears both. Omitted, **provenance follows the text**: it stays if
    `about_me` is unchanged and clears if it changes, so a form resending the
    same bio keeps the record and a rewritten one loses it. A new row (`stored`
    false) has no record to keep. The rules on what
    may be sent are `provenance_problem`'s, checked before this runs.
    """
    drafted = payload.get("about_me_drafted")
    if drafted is True:
        return {
            "about_me_drafted_at": func.now(),
            "about_me_prompt_version": payload["about_me_prompt_version"],
        }
    if drafted is False:
        # Nothing to clear on a profile not yet written; a row made only for
        # this would flip `/me`'s `profile` from null.
        return {"about_me_drafted_at": None, "about_me_prompt_version": None} if stored else {}
    if "about_me" not in payload or not stored:
        return {}
    same = UserProfile.about_me.is_not_distinct_from(payload["about_me"])
    return {
        "about_me_drafted_at": case((same, UserProfile.about_me_drafted_at), else_=None),
        "about_me_prompt_version": case((same, UserProfile.about_me_prompt_version), else_=None),
    }


async def _choose_avatar_focus(
    session: AsyncSession, user_id: UUID, focus: dict[str, float]
) -> None:
    """Record the mentor's own crop of the photo they have now (#214).

    **Conditional on there being a photo**, in the same statement that writes:
    the existence check is atomic. It proves *a* photo exists, not *which* —
    an upload landing at the same moment may reset this crop or receive it.
    Either way the result is cosmetic and visible only to the owner, who can
    choose again, so identity is not pinned.
    """
    result = await session.execute(
        update(UserProfile)
        .where(UserProfile.user_id == user_id, UserProfile.avatar_url.is_not(None))
        .values(**chosen_focus_columns(focus["x"], focus["y"]))
    )
    if not _rowcount(result):
        message = "upload a photo before choosing where to centre it"
        raise ValidationError(message, field_errors=(("/avatar_focus", message),))


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
    held = {
        row.language_id: {"proficiency": row.proficiency, "is_primary": row.is_primary}
        for row in await session.execute(
            select(UserLanguage.language_id, UserLanguage.proficiency, UserLanguage.is_primary)
            .where(UserLanguage.user_id == user_id)
            .with_for_update()
        )
    }
    rows = resolved_languages(entries, held)
    await session.execute(delete(UserLanguage).where(UserLanguage.user_id == user_id))
    if rows:
        await session.execute(insert(UserLanguage), [{"user_id": user_id, **row} for row in rows])


#: What a language newly added to the list starts with.
NEW_LANGUAGE: dict[str, Any] = {
    "proficiency": LanguageProficiency.FLUENT,
    "is_primary": False,
}


def resolved_languages(
    entries: list[dict[str, Any]], held: dict[Any, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Each entry with its omitted fields filled: stored value, else the default.

    An explicit `is_primary: true` anywhere moves the primary there, so every
    other entry resolves to `false` — otherwise a kept primary and a new one
    would both be written, and the one-primary index would refuse the save.
    """
    moved = any(entry.get("is_primary") is True for entry in entries)
    rows = []
    for entry in entries:
        row = {**NEW_LANGUAGE, **held.get(entry["language_id"], {}), **entry}
        if moved and entry.get("is_primary") is not True:
            row["is_primary"] = False
        rows.append(row)
    return rows
