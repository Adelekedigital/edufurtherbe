"""A signed-in mentee's mentor matches (`/me/mentor-matches`, decision 265)."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Query
from pydantic import StringConstraints

from app.api.deps.core import BookingWindowDep, CurrentUserDep, SessionDep, logger
from app.api.schemas.common import StorableText
from app.api.schemas.matches import (
    DegreeBasisRead,
    MatchBasisRead,
    MatchRefRead,
    MentorMatchesRead,
    MentorMatchRead,
)
from app.domain.matching import Mentee, Mentor, Ref, best_field, is_exact, rank, reasons
from app.domain.search import fold
from app.infra.db.mentor_match_store import field_similarities, match_candidates, mentee_answers

#: How many matches a caller may ask for, and the default.
MIN_MATCHES, MAX_MATCHES, DEFAULT_MATCHES = 1, 10, 2
#: Longer than any id or slug `/mentors/{handle}` resolves.
MAX_HANDLE_LENGTH = 128


def _ref(id_: Any, label: Any) -> Ref | None:
    return None if id_ is None or label is None else Ref(UUID(str(id_)), str(label))


def _field_texts(row: dict[str, Any]) -> list[str]:
    """The mentor's courses and programmes, then their primary programme."""
    texts = [*(row["courses"] or ()), *(row["programs"] or ()), row["primary_study_program"]]
    return [t for t in texts if t]


def _mentor(row: dict[str, Any], scores: dict[str, float]) -> Mentor:
    """One candidate row as the ranking's facts."""
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
        has_open_slot=row["next_available_state"] == "open",
    )


def _basis(answers: dict[str, Any], mentee: Mentee) -> MatchBasisRead:
    degree = (
        DegreeBasisRead(
            id=None if answers["degree_id"] is None else str(answers["degree_id"]),
            label=answers["degree_label"],
            raw=answers["degree_goal_raw"],
        )
        if answers["degree_id"] is not None or answers["degree_goal_raw"]
        else None
    )
    return MatchBasisRead(
        degree=degree,
        field=answers["field_of_study"],
        country=MatchRefRead.of(mentee.countries[0] if mentee.countries else None),
        origin=MatchRefRead.of(mentee.origin),
    )


async def mentor_matches(
    user: CurrentUserDep,
    session: SessionDep,
    window: BookingWindowDep,
    limit: Annotated[
        int,
        Query(ge=MIN_MATCHES, le=MAX_MATCHES, description="How many matches, 1 to 10."),
    ] = DEFAULT_MATCHES,
    for_mentor: Annotated[
        str | None,
        Query(
            description=(
                "A mentor's id or slug, as `/mentors/{handle}` takes it. Returns only "
                "that mentor, with reasons, when you can book them."
            )
        ),
        StringConstraints(max_length=MAX_HANDLE_LENGTH),
        StorableText,
    ] = None,
) -> MentorMatchesRead:
    """Rank every mentor the caller could book against the caller's own answers."""
    viewer: UUID = user["id"]
    answers = await mentee_answers(session, viewer)
    mentee = Mentee(
        goal_offering_ids=frozenset(answers["goal_offering_ids"] or ()),
        field=fold(answers["field_of_study"] or "") or None,
        origin=_ref(answers["origin_id"], answers["origin_label"]),
        countries=tuple(
            ref for ref in (_ref(i, label) for i, label in answers["countries"] or ()) if ref
        ),
        degree_rank=answers["degree_rank"],
    )
    handle = (for_mentor or "").strip() or None
    rows = await match_candidates(
        session, viewer, window=window, day=dt.datetime.now(dt.UTC).date(), for_mentor=handle
    )

    scores: dict[str, float] = {}
    if mentee.field is not None:
        texts = {fold(t) for row in rows for t in _field_texts(row)} - {""}
        scores = await field_similarities(session, mentee.field, sorted(texts))

    by_profile = {row["cursor_id"]: row for row in rows}
    ranked = rank(mentee, [_mentor(row, scores) for row in rows])
    chosen = ranked[:limit]
    bookable: bool | None = None
    if handle is not None:
        target = [m for m in ranked if by_profile[m.profile_id]["is_for_mentor"]]
        bookable = bool(target)
        chosen = target[:1] if target else chosen

    exact = is_exact(mentee, chosen[0] if chosen else None)
    # Measured by product as the share of `exact = false`. Ids and counts only.
    logger.info(
        "mentor matches served",
        extra={
            "exact": exact,
            "match_count": len(chosen),
            "candidate_count": len(rows),
            "for_mentor_bookable": bookable,
        },
    )
    return MentorMatchesRead(
        matches=[
            MentorMatchRead.of(
                by_profile[m.profile_id], reasons(mentee, m), m.level, m.study_country
            )
            for m in chosen
        ],
        exact=exact,
        based_on=_basis(answers, mentee),
        for_mentor_bookable=bookable,
    )


MentorMatchesDep = Annotated[MentorMatchesRead, Depends(mentor_matches)]
