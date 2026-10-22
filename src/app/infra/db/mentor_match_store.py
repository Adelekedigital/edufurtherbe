"""The facts mentor matching ranks on (decision 265), in three bounded reads.

1. `mentee_answers`: the caller's own answers, one statement scoped to them;
   `mentee_of` turns them into the ranking's `Mentee`.
2. `match_candidates`: every mentor the caller could book now, as Explore
   cards (`bookable_cards`) with the facts the ranking reads, in one
   statement, plus `offerings_for`'s one batch.
3. `field_scores`: the mentee's field against each distinct folded field
   text by Explore's own prefix and near tiers, one `unnest` statement, run
   only when the mentee gave a field.

The ranking itself is `domain.matching`, in memory.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import ARRAY, Text, bindparam, case, false, func, literal, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.availability import BookingWindow
from app.domain.matching import Mentee, Mentor, Ref, best_field
from app.domain.search import fold
from app.infra.db.mentor_public_store import by_handle
from app.infra.db.mentor_search_store import (
    bookable_cards,
    daily_shuffle,
    field_document,
    near_tier,
    prefix_query,
)
from app.infra.db.models.education import DegreeLevel, EducationEntry
from app.infra.db.models.mentoring import MenteeGoal, MenteeGoalCountry, MentorProfile
from app.infra.db.models.reference import Country
from app.infra.db.models.user import User, UserProfile
from app.infra.db.next_available_state import OPEN
from app.infra.db.offerings import live_goal_offering_ids, offerings_for

__all__ = ["Candidate", "field_scores", "match_candidates", "mentee_answers", "mentee_of"]

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


def _ref(id_: Any, label: Any) -> Ref | None:
    return None if id_ is None or label is None else Ref(UUID(str(id_)), str(label))


def mentee_of(answers: dict[str, Any]) -> Mentee:
    """`mentee_answers` as the ranking's facts; the field folded for comparison."""
    return Mentee(
        goal_offering_ids=frozenset(answers["goal_offering_ids"] or ()),
        field=fold(answers["field_of_study"] or "") or None,
        origin=_ref(answers["origin_id"], answers["origin_label"]),
        countries=tuple(
            ref for ref in (_ref(i, label) for i, label in answers["countries"] or ()) if ref
        ),
        degree_rank=answers["degree_rank"],
    )


@dataclass(frozen=True)
class Candidate:
    """One bookable mentor: the ranking's facts and the card row they came from."""

    mentor: Mentor
    card: dict[str, Any]
    is_for_mentor: bool


def _field_texts(row: dict[str, Any]) -> list[str]:
    """The mentor's courses and programmes, then their primary programme."""
    texts = [*(row["courses"] or ()), *(row["programs"] or ()), row["primary_study_program"]]
    return [t for t in texts if t]


def _mentor(row: dict[str, Any], scores: dict[str, float]) -> Mentor:
    best = best_field((raw, scores.get(fold(raw))) for raw in _field_texts(row))
    return Mentor(
        profile_id=row["cursor_id"],
        shuffle=str(row["shuffle"]),
        offering_ids=tuple(o["id"] for o in row["offerings"]),
        field_similarity=None if best is None else best[0],
        field_text=None if best is None else best[1],
        origin=_ref(row["origin_country_id"], row["origin_country"]),
        study_country=_ref(row["primary_study_country_id"], row["primary_study_country"]),
        level=_ref(row["degree_level_id"], row["degree_level_label"]),
        level_rank=row["degree_level_rank"],
        has_open_slot=row["next_available_state"] == OPEN,
    )


async def match_candidates(
    session: AsyncSession,
    viewer: UUID,
    mentee: Mentee,
    *,
    window: BookingWindow,
    day: dt.date,
    for_mentor: str | None = None,
) -> list[Candidate]:
    """Every mentor `viewer` could book now, never `viewer`, with their facts.

    Each card is an Explore card (`offerings` attached as `/mentors` attaches
    them); `is_for_mentor` marks the mentor the handle names, resolved by
    `by_handle` as `/mentors/{handle}` resolves it.
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
    scores: dict[str, float] = {}
    if mentee.field is not None:
        texts = {fold(t) for row in rows for t in _field_texts(row)} - {""}
        scores = await field_scores(session, mentee.field, sorted(texts))
    return [Candidate(_mentor(row, scores), row, bool(row["is_for_mentor"])) for row in rows]


async def field_scores(session: AsyncSession, field: str, texts: Sequence[str]) -> dict[str, float]:
    """Each of `texts` the mentee's `field` matches, with its score; others absent.

    **Explore's own tiers, not a copy** (`prefix_query`, `near_tier`): every
    word present as a prefix scores 1; otherwise a near spelling, where each
    word of four or more letters must clear `FUZZY_FLOOR` by itself and the
    weakest word is the score. So "Mechanical Engineering" does not match
    "Civil Engineering" on the shared word, and the floor is compared in SQL
    exactly as Explore compares it. Both sides arrive folded
    (`domain.search.fold`), which makes it accent-insensitive.
    """
    if not texts:
        return {}
    value = (
        func.unnest(bindparam("texts", list(texts), type_=ARRAY(Text)))
        .table_valued("value")
        .render_derived(name="field_text")
    )
    document = field_document(value.c.value)
    whens = []
    prefix = prefix_query(field)
    if prefix is not None:
        whens.append((document.op("@@")(prefix), literal(1.0)))
    near = near_tier(field, value.c.value, document)
    if near is not None:
        whens.append(near)
    if not whens:
        return {}
    statement = select(value.c.value, case(*whens, else_=None).label("score"))
    rows = await session.execute(statement)
    return {row.value: float(row.score) for row in rows if row.score is not None}
