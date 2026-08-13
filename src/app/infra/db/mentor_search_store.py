"""Finding a mentor — the only endpoint that answers "who is there at all".

Three public reads existed before this and every one of them needed an id or a
slug you already had. This is the one that hands them out.

**Bookable, never available.** The scope is `mentor_is_public()` plus
`mentor_is_bookable()`: approved, listed, not deleted either way, and set up —
a live session type with a booking config, and a live availability rule. It
says nothing about *when*, because availability is a computation over projected
windows minus bookings and cannot be a `WHERE` clause. Filtering on it would
mean computing slots for every candidate before paging, which stops the cursor
being a database keyset; and caching it in a column is the drift D20 rejected,
where a stored `is_available` was wrong the moment somebody booked. *When* is
what `/slots` answers, freshly, one click later.

**No filters.** Service, school, degree, country of study and country of origin
are all reachable from existing tables, and four of the indexes they would want
already exist. They are not here because a query parameter is additive and a
sort order is not (rule #21) — what has to be right today is the shape a client
renders and the cursor it pages with. Three of those five filters read through
`education_entries`, which is one-to-many, so they arrive as `EXISTS` clauses
rather than joins when they arrive at all.

**Ordered by `mentor_profiles.id`, not `users.id`.** Both are UUIDv7 and both are
therefore time-ordered, but they order different events: when somebody signed up
against when they became a mentor. A mentee of two years who started mentoring
last week is a new mentor, and this list is of mentors.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.reference import Country
from app.infra.db.models.user import User, UserProfile
from app.infra.db.offerings import offerings_for
from app.infra.db.public_visibility import mentor_is_bookable, mentor_is_public

__all__ = ["search_mentors"]

_STUDY_COUNTRY = Country.__table__.alias("study_country")


def _page(after: UUID | None, limit: int) -> Select[Any]:
    """One page of mentors, newest first.

    `mentor_profiles.id` is both the sort key and the cursor, which is ADR 0016's
    base case — *"the id is the cursor when the display order is the id order"* —
    and it is returned as `cursor_id` rather than left implicit. The row's own
    `id` is the **user**, so the two are different values and the caller must not
    reach for the visible one when building the next token.
    """
    return (
        select(
            User.id.label("user_id"),
            MentorProfile.id.label("cursor_id"),
            User.slug,
            User.first_name,
            User.last_name,
            MentorProfile.headline,
            MentorProfile.years_of_experience,
            UserProfile.avatar_url,
            _STUDY_COUNTRY.c.display_name.label("primary_study_country"),
        )
        .select_from(MentorProfile)
        .join(User, User.id == MentorProfile.user_id)
        # Outer: a mentor who never wrote a bio has no `user_profiles` row at all,
        # and an inner join would make them unfindable while their profile page
        # works perfectly — invisible in the one place a mentee looks.
        .outerjoin(UserProfile, UserProfile.user_id == MentorProfile.user_id)
        .outerjoin(_STUDY_COUNTRY, _STUDY_COUNTRY.c.id == MentorProfile.primary_study_country_id)
        .where(
            *mentor_is_public(),
            *mentor_is_bookable(),
            *([MentorProfile.id < after] if after is not None else []),
        )
        .order_by(MentorProfile.id.desc())
        .limit(limit + 1)
    )


async def search_mentors(
    session: AsyncSession, *, limit: int, after: UUID | None = None
) -> tuple[list[dict[str, Any]], bool]:
    """One page of bookable mentors, and whether another follows.

    Two statements, not one per mentor. The offerings are fetched for the whole
    page in a single query and attached afterwards — written per-row it was
    twenty round trips a page, and the obvious fix of a second batched function
    beside the single one is two queries of one rule.

    One more row than asked for is fetched: if it comes back there is a next
    page. Cheaper and more honest than a second `COUNT`, which can disagree with
    the page it claims to describe.
    """
    rows = [dict(r) for r in (await session.execute(_page(after, limit))).mappings()]
    page, has_more = rows[:limit], len(rows) > limit

    grouped = await offerings_for(session, [row["user_id"] for row in page])
    for row in page:
        row["offerings"] = grouped.get(row["user_id"], [])
    return page, has_more
