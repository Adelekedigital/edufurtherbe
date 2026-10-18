"""The facts mentor matching ranks on (decision 265), in three bounded reads.

1. `mentee_answers`: the caller's own answers, one statement scoped to them.
2. `match_candidates`: every mentor the caller could book now, as Explore
   cards (`bookable_cards`) with the facts the ranking reads, in one
   statement, plus `offerings_for`'s one batch.
3. `field_similarities`: pg_trgm `strict_word_similarity` of the mentee's
   field against each distinct folded field text, one `unnest` statement, run
   only when the mentee gave a field.

The ranking itself is `domain.matching`, in memory.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import false, func, select, text
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.availability import BookingWindow
from app.infra.db.mentor_public_store import by_handle
from app.infra.db.mentor_search_store import bookable_cards, daily_shuffle
from app.infra.db.models.education import DegreeLevel, EducationEntry
from app.infra.db.models.mentoring import MenteeGoal, MenteeGoalCountry, MentorProfile
from app.infra.db.models.reference import Country
from app.infra.db.models.user import User, UserProfile
from app.infra.db.offerings import live_goal_offering_ids, offerings_for

__all__ = ["field_similarities", "match_candidates", "mentee_answers"]

_ORIGIN = Country.__table__.alias("mentee_origin")


async def mentee_answers(session: AsyncSession, viewer: UUID) -> dict[str, Any]:
    """The caller's goal offerings, field, degree goal, destinations and origin.

    One statement, from the caller's own `users` row outward, so every answer
    is theirs: no other user's id is bound. `countries` is `[[id, label], ...]`
    in the mentee's choice order (`priority` ascending), or null.
    """
    pair = func.json_build_array(Country.id, Country.display_name)
    countries = (
        select(
            func.json_agg(
                aggregate_order_by(pair, MenteeGoalCountry.priority, MenteeGoalCountry.id)
            )
        )
        .select_from(MenteeGoalCountry)
        .join(Country, Country.id == MenteeGoalCountry.country_id)
        .where(MenteeGoalCountry.user_id == viewer)
        .scalar_subquery()
    )
    statement = (
        select(
            live_goal_offering_ids(viewer).label("goal_offering_ids"),
            MenteeGoal.field_of_study,
            MenteeGoal.degree_goal_raw,
            DegreeLevel.id.label("degree_id"),
            DegreeLevel.display_name.label("degree_label"),
            DegreeLevel.sort_order.label("degree_rank"),
            countries.label("countries"),
            _ORIGIN.c.id.label("origin_id"),
            _ORIGIN.c.display_name.label("origin_label"),
        )
        .select_from(User)
        .outerjoin(MenteeGoal, MenteeGoal.user_id == User.id)
        .outerjoin(DegreeLevel, DegreeLevel.id == MenteeGoal.degree_goal_id)
        .outerjoin(UserProfile, UserProfile.user_id == User.id)
        .outerjoin(_ORIGIN, _ORIGIN.c.id == UserProfile.origin_country_id)
        .where(User.id == viewer)
    )
    return dict((await session.execute(statement)).mappings().one())


def _education_texts(column: Any) -> Any:
    """One column of the mentor's live education entries, as an array."""
    return (
        select(func.array_agg(column))
        .select_from(EducationEntry)
        .where(
            EducationEntry.user_id == MentorProfile.user_id,
            EducationEntry.deleted_at.is_(None),
            column.is_not(None),
        )
        .correlate(MentorProfile)
        .scalar_subquery()
    )


async def match_candidates(
    session: AsyncSession,
    viewer: UUID,
    *,
    window: BookingWindow,
    day: dt.date,
    for_mentor: str | None = None,
) -> list[dict[str, Any]]:
    """Every mentor `viewer` could book now, never `viewer`, as card rows.

    Each row is an Explore card (`offerings` attached as `/mentors` attaches
    them) plus `shuffle`, the mentor's field texts (`courses`, `programs`,
    `primary_study_program`) and `is_for_mentor`, true for the mentor the
    handle names, resolved by `by_handle` as `/mentors/{handle}` resolves it.
    """
    statement = bookable_cards(viewer, window).add_columns(
        daily_shuffle(viewer, day).label("shuffle"),
        _education_texts(EducationEntry.study_course).label("courses"),
        _education_texts(EducationEntry.study_program).label("programs"),
        MentorProfile.primary_study_program,
        (by_handle(for_mentor) if for_mentor is not None else false()).label("is_for_mentor"),
    )
    rows = [dict(r) for r in (await session.execute(statement)).mappings()]
    grouped = await offerings_for(session, [row["user_id"] for row in rows])
    for row in rows:
        row["offerings"] = grouped.get(row["user_id"], [])
    return rows


async def field_similarities(
    session: AsyncSession, field: str, texts: Sequence[str]
) -> dict[str, float]:
    """pg_trgm `strict_word_similarity(field, t)` for each of `texts`, keyed by text.

    **Strict word similarity, Explore's near-tier metric**, because `FUZZY_FLOOR`
    was measured on it: the mentee's words found as whole words in the
    mentor's text, so "Computer Science" scores 1.0 inside a long programme
    name (plain `similarity` reads 0.34) while "law" stays apart from
    "lawrence". Both sides arrive folded (`domain.search.fold`), which is what
    makes it accent-insensitive; pg_trgm itself only lowercases.
    """
    if not texts:
        return {}
    statement = text(
        "SELECT u.value, strict_word_similarity(:field, u.value) AS score "
        "FROM unnest(CAST(:texts AS text[])) AS u(value)"
    )
    rows = await session.execute(statement, {"field": field, "texts": list(texts)})
    return {row.value: float(row.score) for row in rows}
