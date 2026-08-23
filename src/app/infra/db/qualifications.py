"""Which single education entry represents a person, as a lateral.

Extracted from `mentor_search_store` when a second caller appeared: a review on
a mentor's profile shows the *reviewer's* institution, and "which institution"
has to mean the same thing for a mentee as it does for a mentor. Two copies of
the ordering rule would drift, and the one that drifts is the one with fewer
tests — which is the new one.

`predicates.py` is the precedent for the move and states the rule it follows:
this module exists because a second store now needs it, which is the
extract-on-the-second-occurrence case non-negotiable #8 names.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from app.infra.db.models.education import DegreeLevel, EducationEntry, Institution

__all__ = ["top_qualification"]

#: Aliased so a lateral can name its columns without colliding with any other
#: join to the same catalogue in the outer query.
_DEGREE_LEVEL = DegreeLevel.__table__.alias("degree_level")


def top_qualification(user: Any, name: str = "top_qualification") -> Any:
    """The one education entry that represents ``user``, as a lateral.

    **Highest level first, then the later end date, then the id.** Not
    `is_most_recent`, which the schema offers and the data does not: the column
    is blank on all 21 rows of the dev export, so `BOOLEANS[""]` makes it `False`
    everywhere and a card keyed on it renders nothing for anybody. Legacy owned
    that flag, had write paths for it, and still never set it — which is what a
    stored value derived from *other rows* does. It is a property of the set, not
    of the row, so anything that adds, edits or deletes a sibling invalidates it
    (D56: derived at query time, never stored).

    **The correlation is a parameter**, because the two callers point it at
    different people: a search card at the mentor it is describing, a review at
    the mentee who wrote it. Closing over one would have meant copying the rest.

    **Level outranks recency** because the card is a credibility signal. A mentor
    who takes a second bachelor's after a doctorate is still a doctor, and
    ordering by date alone would quietly demote them.

    A lateral rather than a join with a `DISTINCT ON`: this returns exactly one
    row by construction, so it cannot multiply the page however many entries a
    user has, and the limit is visible at the point it applies.
    """
    return (
        select(
            func.coalesce(EducationEntry.degree_abbreviation, _DEGREE_LEVEL.c.short_name).label(
                "degree"
            ),
            EducationEntry.study_course.label("study_course"),
            # ADR 0008 point 5: the registry is allowed to be incomplete, so what
            # the user typed is always kept and the link is opportunistic.
            func.coalesce(Institution.name, EducationEntry.school_name_raw).label("institution"),
        )
        .select_from(EducationEntry)
        .outerjoin(Institution, Institution.id == EducationEntry.institution_id)
        .outerjoin(_DEGREE_LEVEL, _DEGREE_LEVEL.c.id == EducationEntry.degree_level_id)
        .where(
            EducationEntry.user_id == user,
            EducationEntry.deleted_at.is_(None),
        )
        .order_by(
            _DEGREE_LEVEL.c.sort_order.desc().nullslast(),
            EducationEntry.date_end.desc().nullslast(),
            EducationEntry.id.desc(),
        )
        .limit(1)
        .lateral(name)
    )
