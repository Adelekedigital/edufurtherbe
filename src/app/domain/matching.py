"""Which bookable mentors suit a mentee best, and why (decision 265).

Pure: the store loads the facts and this module ranks them in memory, which is
fine at the 44 to 500 mentors this platform has. The order is the owner's
(2026-10-17), most important first, and each key beats every key below it:

1. **Help areas**: how many of the mentee's live goal needs the mentor offers.
2. **Field of study**: the best `strict_word_similarity` (Explore's near-tier
   metric) of the mentee's field against the mentor's courses and programmes,
   counted only from `FUZZY_FLOOR` up. So matched beats unmatched, then closer
   beats less close.
3. **Origin**: both from the same country.
4. **A free slot**: the mentor's next free time is `open`. Any open slot
   counts the same; sooner is not better.
5. **Study country**, by the mentee's choice order (1, 1/2, 1/3, ...), then
   **degree level**: the mentor's highest level is the goal (1) or above it (0.5).

Ties go to the per-viewer, per-day md5 shuffle Explore uses, then
`mentor_profiles.id`, newest first. A fact missing on either side scores 0.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from app.domain.search import FUZZY_FLOOR

__all__ = [
    "DEGREE_ABOVE",
    "DEGREE_EXACT",
    "Mentee",
    "Mentor",
    "Reason",
    "Ref",
    "best_field",
    "field_score",
    "is_exact",
    "rank",
    "reasons",
    "score",
]

#: The degree key's scores: the mentor holds the mentee's goal level, or a
#: higher one. Lower or unknown is 0.
DEGREE_EXACT = 1.0
DEGREE_ABOVE = 0.5

ReasonCode = Literal["help_areas", "field", "origin", "study_country", "degree"]


@dataclass(frozen=True)
class Ref:
    """A catalogue row as a client shows it: its id and its label."""

    id: UUID
    label: str


@dataclass(frozen=True)
class Mentee:
    """The caller's own answers. Every one may be missing."""

    goal_offering_ids: frozenset[UUID] = frozenset()
    #: Already folded (`domain.search.fold`); `None` when unanswered.
    field: str | None = None
    origin: Ref | None = None
    #: Destination countries, first choice first. Empty for "Not sure yet".
    countries: tuple[Ref, ...] = ()
    #: `degree_levels.sort_order` of the goal; `None` when unknown.
    degree_rank: int | None = None


@dataclass(frozen=True)
class Mentor:
    """One bookable mentor's facts, as the store loaded them."""

    #: `mentor_profiles.id`: the last tie-break.
    profile_id: UUID
    #: Explore's daily md5 shuffle for this viewer and mentor.
    shuffle: str
    #: The offering ids they give, in the platform's order.
    offering_ids: tuple[UUID, ...] = ()
    #: Their best field similarity to the mentee's, and the text that scored it.
    field_similarity: float | None = None
    field_text: str | None = None
    origin: Ref | None = None
    study_country: Ref | None = None
    level: Ref | None = None
    level_rank: int | None = None
    has_open_slot: bool = False


@dataclass(frozen=True)
class Reason:
    """One signal that matched. Only the fields of its own code are set."""

    code: ReasonCode
    offering_ids: tuple[UUID, ...] = ()
    field: str | None = None
    ref: Ref | None = None


def best_field(scored: Iterable[tuple[str, float | None]]) -> tuple[float, str] | None:
    """The mentor's field text that scored highest, as `(similarity, text)`.

    `scored` is each of their texts (courses, programmes, primary programme, in
    that order) with its similarity, `None` when it was not scored. A tie keeps
    the first, so a course beats an equal programme name.
    """
    best: tuple[float, str] | None = None
    for raw, similarity in scored:
        if similarity is not None and (best is None or similarity > best[0]):
            best = (similarity, raw)
    return best


def field_score(similarity: float | None) -> float:
    """The field key: the similarity when it clears `FUZZY_FLOOR`, else 0."""
    if similarity is None or similarity < FUZZY_FLOOR:
        return 0.0
    return similarity


def _field(mentee: Mentee, mentor: Mentor) -> float:
    """The field key; nothing when the mentee gave no field."""
    return 0.0 if mentee.field is None else field_score(mentor.field_similarity)


def _shared(mentee: Mentee, mentor: Mentor) -> tuple[UUID, ...]:
    return tuple(o for o in mentor.offering_ids if o in mentee.goal_offering_ids)


def _same_origin(mentee: Mentee, mentor: Mentor) -> bool:
    return (
        mentee.origin is not None
        and mentor.origin is not None
        and mentee.origin.id == mentor.origin.id
    )


def _study_country_score(mentee: Mentee, mentor: Mentor) -> float:
    if mentor.study_country is None:
        return 0.0
    for position, country in enumerate(mentee.countries, start=1):
        if country.id == mentor.study_country.id:
            return 1.0 / position
    return 0.0


def _degree_score(mentee: Mentee, mentor: Mentor) -> float:
    if mentee.degree_rank is None or mentor.level_rank is None:
        return 0.0
    if mentor.level_rank == mentee.degree_rank:
        return DEGREE_EXACT
    if mentor.level_rank > mentee.degree_rank:
        return DEGREE_ABOVE
    return 0.0


def score(mentee: Mentee, mentor: Mentor) -> tuple[int, float, int, int, float, float]:
    """The six ranking keys, most important first; compared as a tuple."""
    return (
        len(_shared(mentee, mentor)),
        _field(mentee, mentor),
        int(_same_origin(mentee, mentor)),
        int(mentor.has_open_slot),
        _study_country_score(mentee, mentor),
        _degree_score(mentee, mentor),
    )


def rank(mentee: Mentee, mentors: Sequence[Mentor]) -> list[Mentor]:
    """`mentors`, best match first.

    Two stable sorts: the tie-breaks first (shuffle ascending, then the newest
    profile), then the score descending, which keeps the tie order inside
    equal scores.
    """
    ordered = sorted(mentors, key=lambda m: m.profile_id, reverse=True)
    ordered.sort(key=lambda m: m.shuffle)
    ordered.sort(key=lambda m: score(mentee, m), reverse=True)
    return ordered


def reasons(mentee: Mentee, mentor: Mentor) -> list[Reason]:
    """The signals that matched, in ranking order. A free slot is never one."""
    found: list[Reason] = []
    shared = _shared(mentee, mentor)
    if shared:
        found.append(Reason("help_areas", offering_ids=shared))
    if _field(mentee, mentor) > 0 and mentor.field_text:
        found.append(Reason("field", field=mentor.field_text))
    if _same_origin(mentee, mentor):
        found.append(Reason("origin", ref=mentor.origin))
    if _study_country_score(mentee, mentor) > 0:
        found.append(Reason("study_country", ref=mentor.study_country))
    if _degree_score(mentee, mentor) > 0:
        found.append(Reason("degree", ref=mentor.level))
    return found


def is_exact(mentee: Mentee, top: Mentor | None) -> bool:
    """Whether the top match meets every primary signal the mentee answered.

    The primary signals are the first three keys: help areas (at least one
    shared), field and origin. One the mentee has no answer for is not asked.
    **A mentee who answered none of the three gets `False`**, as does an empty
    list: there is nothing an exact match could be exact about.
    """
    if top is None:
        return False
    asked = [
        (bool(mentee.goal_offering_ids), bool(_shared(mentee, top))),
        (mentee.field is not None, _field(mentee, top) > 0),
        (mentee.origin is not None, _same_origin(mentee, top)),
    ]
    answered = [met for has_answer, met in asked if has_answer]
    return bool(answered) and all(answered)
