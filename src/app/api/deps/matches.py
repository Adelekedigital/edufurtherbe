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
from app.domain.matching import Mentee, is_exact, reasons, select_matches
from app.infra.db.mentor_match_store import match_candidates, mentee_answers, mentee_of

#: How many matches a caller may ask for, and the default.
MIN_MATCHES, MAX_MATCHES, DEFAULT_MATCHES = 1, 10, 2
#: Longer than any id or slug `/mentors/{handle}` resolves.
MAX_HANDLE_LENGTH = 128


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
    mentee = mentee_of(answers)
    handle = (for_mentor or "").strip() or None
    candidates = await match_candidates(
        session,
        viewer,
        mentee,
        window=window,
        day=dt.datetime.now(dt.UTC).date(),
        for_mentor=handle,
    )
    targets = (
        None
        if handle is None
        else frozenset(c.mentor.profile_id for c in candidates if c.is_for_mentor)
    )
    chosen, bookable = select_matches(mentee, [c.mentor for c in candidates], limit, targets)
    cards = {c.mentor.profile_id: c.card for c in candidates}

    exact = is_exact(mentee, chosen[0] if chosen else None)
    # Measured by product as the share of `exact = false`. Ids and counts only.
    logger.info(
        "mentor matches served",
        extra={
            "exact": exact,
            "match_count": len(chosen),
            "candidate_count": len(candidates),
            "for_mentor_bookable": bookable,
        },
    )
    return MentorMatchesRead(
        matches=[
            MentorMatchRead.of(cards[m.profile_id], reasons(mentee, m), m.level, m.study_country)
            for m in chosen
        ],
        exact=exact,
        based_on=_basis(answers, mentee),
        for_mentor_bookable=bookable,
    )


MentorMatchesDep = Annotated[MentorMatchesRead, Depends(mentor_matches)]
