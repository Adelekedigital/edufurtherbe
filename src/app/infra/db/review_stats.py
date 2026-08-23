"""What a mentor's reviews add up to, derived every time they are asked.

**Nothing here is stored, and D56 is why**: *"counts and ratios are derived at
query time, never stored — no cached totals, no denormalised counters, no
percentage columns."* The same rule `session_stats` follows, for the same
reason: an average is a property of the *set*, so anything that adds, edits or
withdraws a sibling invalidates a stored copy.

ONE PREDICATE, TWO QUESTIONS
============================
The discovery card shows a review count and a session value; the profile shows
those plus four percentages and a recommendation figure. They are the same
numbers about the same mentor from the same rows, so there is one
`review_summary()` and each caller takes what it needs — the shape
`session_stats.delivered()` established when the card and the profile both
needed "what counts as delivered".

It takes its mentor as a parameter because the two callers identify them
differently: the card correlates against a column inside a paged query, the
profile passes a resolved id.

**A lateral rather than one scalar subquery per figure.** `_completed_sessions()`
is a scalar subquery because it is *one* value; `_top_qualification()` is a
lateral because it is three columns from the same rows. This is six from the
same rows, so it is the second shape — and the difference is real work: six
scalar subqueries would scan the same index six times per card.

WHAT AN EMPTY ANSWER LOOKS LIKE, AND WHY IT IS NOT ONE ANSWER
=============================================================
`review_count` is **zero**; every average and percentage is **null**. That is not
an inconsistency, it is the rule this codebase already applies twice:

    completed_sessions   0      "zero is a real answer"
    attendance_rate      null   "zero percent says never shows up"

A count over no rows *is* nought. A ratio over no rows is unknown, and rendering
a brand-new mentor as nought out of five would be a lie the card cannot take
back.

THE PERCENTAGE IS THE API'S, NOT THE CLIENT'S
=============================================
Both the average and the percentage are published, derived here, and pinned to
each other by a test. Publishing only the average would put the mapping in every
client that renders it — the second copy of one rule that non-negotiable #8
calls a defect — and publishing only the percentage would lose the ordinal a
future display might want.

**Rounded in SQL, never at the boundary.** `attendance_rate` records why:
`round()` returns numeric, so a Python-side `int()` refuses the value that
arrives. It matters twice here, because PostgreSQL rounds halves *away from
zero* and Python rounds them *to even* — `round(2.5)` is 3 in one and 2 in the
other. One rounding, in one place, or the two disagree on exactly the values a
three-point scale produces most often.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Integer, Numeric, Select, and_, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import SessionRole
from app.domain.reviews import MENTOR_RATINGS, ORDINAL_SCALE, RECOMMEND_SCALE, VALUABLE_SCALE
from app.infra.db.models.reviews import Review

__all__ = ["card_summary", "mentor_review_stats", "profile_summary", "published"]

#: The top of each scale, which is what a percentage divides by. Register
#: question 2, answered from the display: `97% Recommended` over three reviews
#: can only come from `mean / max` — three people cannot produce 97% as a
#: promoter fraction, and a normalised `(v-1)/(max-1)` would render 96%.
_ORDINAL_MAX = ORDINAL_SCALE[1]
_RECOMMEND_MAX = RECOMMEND_SCALE[1]
_VALUABLE_MAX = VALUABLE_SCALE[1]


def published(mentor: Any) -> Any:
    """The reviews that count towards a mentor's public figures.

    **Character for character the predicate of `ix_reviews_mentor_valuable`**,
    which is partial on `deleted_at IS NULL AND reviewed_for_role = 'mentor'`. If
    the two drift the index silently stops applying and nothing fails — the
    query still answers, just by reading the table.

    `reviewed_for_role` is not redundant with `reviewed_for`. That column names a
    *user*, and a user is a mentor to one person and a mentee to another; the day
    a mentor reviews a mentee, an aggregate filtering on the id alone starts
    counting the wrong rows.

    **Withdrawn reviews are excluded here and counted everywhere else.** A review
    taken down by moderation must not move an average — but it still happened,
    so it still holds its session's slot and still suppresses the interval.
    """
    return and_(
        Review.reviewed_for == mentor,
        Review.reviewed_for_role == SessionRole.MENTOR,
        Review.deleted_at.is_(None),
    )


def _average(column: Any) -> Any:
    """A mean to two places, or ``NULL`` over no rows.

    Two places rather than one because the percentage is derived from this
    number: a display rounding would make the pair fail its own drift test on
    values a three-point scale reaches routinely.
    """
    return cast(func.round(cast(func.avg(column), Numeric), 2), Numeric(5, 2))


def _percent(column: Any, high: int) -> Any:
    """A whole-number percentage of the scale's top, or ``NULL`` over no rows.

    `NULL` propagates through `avg()` on its own, so no `CASE` is needed to
    produce it — an average over nothing is already unknown, and dividing it
    keeps it unknown.
    """
    return cast(func.round(100.0 * func.avg(column) / high), Integer)


def card_summary(mentor: Any) -> Select[Any]:
    """The two figures a discovery card shows: how many, and how valuable.

    **Deliberately narrower than the profile's, and the index is why.**
    `ix_reviews_mentor_valuable` covers `(reviewed_for, valuable_rating)` under
    the same partial predicate `published()` states, so these two are answered by
    an index-only scan — measured at zero heap fetches over 36,000 reviews.
    Adding the four ordinals or the recommend score here would put columns the
    index does not carry into a query that runs **once per card**, turning twenty
    index-only scans into twenty heap scans on the page a mentee lands on.

    That is a different question from the profile's, not a smaller copy of it.
    What the two share is `published()` and the rounding helpers, which is where
    the rules actually live.
    """
    return (
        select(
            func.count().label("review_count"),
            _average(Review.valuable_rating).label("session_value"),
        )
        .select_from(Review)
        .where(published(mentor))
    )


def profile_summary(mentor: Any) -> Select[Any]:
    """Every public figure about one mentor's reviews, in one pass.

    Six aggregates over the same rows, which is why this is a lateral rather than
    six correlated scalar subqueries: those would scan the same rows six times.
    `_top_qualification()` is the precedent — a lateral when several columns come
    from one set, a scalar subquery when it is one value.

    Read once per profile, so the heap fetches the four ordinals and the
    recommend score require are paid on a page that renders one mentor.
    """
    columns: list[Any] = [
        func.count().label("review_count"),
        _average(Review.valuable_rating).label("session_value"),
        _percent(Review.valuable_rating, _VALUABLE_MAX).label("session_value_percent"),
        _percent(Review.nps_recommend_score, _RECOMMEND_MAX).label("recommended_percent"),
    ]
    for rating in MENTOR_RATINGS:
        column = getattr(Review, rating)
        columns.append(_average(column).label(f"{rating}_average"))
        columns.append(_percent(column, _ORDINAL_MAX).label(f"{rating}_percent"))

    return select(*columns).select_from(Review).where(published(mentor))


async def mentor_review_stats(session: AsyncSession, mentor: UUID) -> dict[str, Any]:
    """The profile's block, resolved.

    Aggregates over no rows still produce a row, so a mentor nobody has reviewed
    comes back as a count of zero and nulls rather than as `None` — which is why
    this returns a dict and the caller needs no fallback.
    """
    row = (await session.execute(profile_summary(mentor))).mappings().one()
    return dict(row)
