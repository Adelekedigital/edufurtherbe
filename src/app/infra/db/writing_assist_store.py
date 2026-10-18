"""The writing assistant's reads and its one write (decision 267).

**Every statement is scoped to the caller's own id** (non-negotiable #5): the
facts a draft is written from are the caller's, and the daily count is theirs.

**The facts are labels, never identity.** Country and degree names, the field of
study and the help areas, read from the caller's saved profile; no name, email,
id, or free text the caller wrote elsewhere (a goal's notes, a headline).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import exists, func, insert, literal, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import RateLimitedError
from app.domain.enums import WritingAssistField
from app.domain.writing_assist import DAILY_WINDOW, window_retry_after
from app.infra.db.models.mentoring import MenteeGoal, MentorProfile
from app.infra.db.models.reference import Country
from app.infra.db.models.user import UserProfile
from app.infra.db.models.writing_assist import WritingAssistRequest
from app.infra.db.offerings import offerings_for
from app.infra.db.profile_store import get_goal

__all__ = [
    "WRITING_ASSIST_LOCK_NAMESPACE",
    "draft_facts",
    "has_required_profile",
    "record_call",
]

#: Advisory-lock namespace for the daily count: `WAST` as four ASCII bytes.
WRITING_ASSIST_LOCK_NAMESPACE = 0x57415354


async def _origin(session: AsyncSession, user_id: UUID) -> str | None:
    return (
        await session.execute(
            select(Country.display_name)
            .join(UserProfile, UserProfile.origin_country_id == Country.id)
            .where(UserProfile.user_id == user_id)
        )
    ).scalar()


async def _mentee_facts(session: AsyncSession, user_id: UUID) -> dict[str, Any]:
    """From the goal the profile reads (`get_goal`), never a second query of it;
    its notes are free text and stay out."""
    goal = await get_goal(session, user_id) or {}
    return {
        "origin": await _origin(session, user_id),
        "degree": goal.get("degree_goal_name") or goal.get("degree_goal_raw"),
        "field_of_study": goal.get("field_of_study"),
        "target_start": goal.get("target_start_term"),
        "destinations": [country["display_name"] for country in goal.get("countries", [])],
        "help_areas": [need["display_name"] for need in goal.get("needs", [])],
    }


async def _mentor_facts(session: AsyncSession, user_id: UUID) -> dict[str, Any]:
    """The mentor profile's labels, and the live offerings as every profile
    reads them (`offerings_for`); the headline is free text and stays out."""
    mentor = (
        await session.execute(
            select(
                Country.display_name,
                MentorProfile.primary_study_program,
                MentorProfile.years_of_experience,
            )
            .select_from(MentorProfile)
            .outerjoin(Country, Country.id == MentorProfile.primary_study_country_id)
            .where(MentorProfile.user_id == user_id, MentorProfile.deleted_at.is_(None))
        )
    ).first()
    offerings = (await offerings_for(session, [user_id])).get(user_id, [])
    study_country, program, years = mentor if mentor is not None else (None, None, None)
    return {
        "origin": await _origin(session, user_id),
        "study_country": study_country,
        "study_program": program,
        "years_of_experience": years,
        "help_areas": [offering["display_name"] for offering in offerings],
    }


async def draft_facts(
    session: AsyncSession, user_id: UUID, field: WritingAssistField
) -> dict[str, Any] | None:
    """The labels a draft is written from, or `None` when the caller lacks the
    profile the field needs: a live mentor profile for `mentor_about`, a goal or
    a profile for `mentee_bio`."""
    if not await has_required_profile(session, user_id, field):
        return None
    if field is WritingAssistField.MENTOR_ABOUT:
        return await _mentor_facts(session, user_id)
    return await _mentee_facts(session, user_id)


async def has_required_profile(
    session: AsyncSession, user_id: UUID, field: WritingAssistField
) -> bool:
    """Whether the caller has the profile the field belongs to (see `draft_facts`)."""
    found: Any = (
        exists().where(MentorProfile.user_id == user_id, MentorProfile.deleted_at.is_(None))
        if field is WritingAssistField.MENTOR_ABOUT
        else exists().where(MenteeGoal.user_id == user_id)
        | exists().where(UserProfile.user_id == user_id)
    )
    return bool((await session.execute(select(found))).scalar())


def _window(user_id: UUID, field: WritingAssistField) -> Any:
    return (
        (WritingAssistRequest.user_id == user_id)
        & (WritingAssistRequest.field == field)
        & (WritingAssistRequest.created_at > func.now() - literal(DAILY_WINDOW))
    )


async def record_call(
    session: AsyncSession, user_id: UUID, field: WritingAssistField, *, limit: int
) -> tuple[UUID, int]:
    """Count the caller's calls for `field` in the window and record one more,
    or refuse past `limit`. Returns the new row's id and the call's `attempt`.

    **Serialized per user and field** by a transaction advisory lock, so two
    presses at once cannot both pass the limit; the caller commits before the
    call is made, which releases it. Counted on the database's clock.
    `attempt` counts every call ever made for the field, so no two calls share
    a body or an idempotency key.

    `Retry-After` waits until enough calls leave the window to bring the count
    under `limit`, as for intake uploads (decision 235).
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:ns, hashtext(:key))"),
        {"ns": WRITING_ASSIST_LOCK_NAMESPACE, "key": f"{user_id}:{field.value}"},
    )
    in_window = _window(user_id, field)
    count, total, now = (
        await session.execute(
            select(
                func.count().filter(in_window),
                func.count(),
                func.now(),
            ).where(
                WritingAssistRequest.user_id == user_id,
                WritingAssistRequest.field == field,
            )
        )
    ).one()
    if count >= limit:
        freeing = (
            await session.execute(
                select(WritingAssistRequest.created_at)
                .where(in_window)
                .order_by(WritingAssistRequest.created_at)
                .offset(count - limit)
                .limit(1)
            )
        ).scalar()
        raise RateLimitedError(
            f"you have asked the writing assistant {limit} times today for this; try again later",
            retry_after_seconds=1 if freeing is None else window_retry_after(freeing, now),
        )
    row_id = (
        await session.execute(
            insert(WritingAssistRequest)
            .values(user_id=user_id, field=field)
            .returning(WritingAssistRequest.id)
        )
    ).scalar_one()
    return row_id, int(total) + 1
