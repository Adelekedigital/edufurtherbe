"""The week's featured mentor: read it, or pick and keep it.

The rules — weighting and rotation — are pure, in `domain/featured.py`. This
module gives them their inputs and keeps their answer.

PICKED ON FIRST REQUEST, UNDER A LOCK
=====================================
There is no weekly job. The first request of a week finds no pick, takes a
transaction-scoped advisory lock, looks again, and only then picks and writes.
A second request racing it waits on the lock, finds the row, and returns it. A
job would need a QStash schedule for something one request a week does anyway,
and it would leave a gap on Monday morning until it ran.

**A write on a public read**, once a week, and deliberately so. It is bounded:
the lock is transaction-scoped and released at commit, and the only thing an
anonymous caller can cause is the one pick the week needs anyway.

THE WEEK'S MENTOR IS THE LATEST STILL-BOOKABLE ROW
==================================================
A mentor featured on Monday who pauses on Wednesday is not shown on Thursday.
Their row stays — they had their turn this cycle — and the next request picks a
replacement from the same rotation.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.featured import Candidate, eligible, pick, week_start
from app.infra.db.mentor_search_store import completed_sessions
from app.infra.db.models.mentoring import FeaturedMentor, MentorProfile
from app.infra.db.models.user import UserProfile
from app.infra.db.public_visibility import bookable_mentors
from app.infra.db.review_stats import card_summary

__all__ = ["current_featured"]

#: The advisory lock the pick is serialised on. A fixed key: there is one
#: featured slot, so there is one lock. Any constant no other lock uses.
PICK_LOCK = 0x46454154  # "FEAT"


async def _week_pick(session: AsyncSession, week: dt.date) -> UUID | None:
    """This week's latest pick who is still bookable, if there is one."""
    return (
        await session.execute(
            select(FeaturedMentor.mentor_user_id)
            .where(
                FeaturedMentor.week_start == week,
                FeaturedMentor.mentor_user_id.in_(bookable_mentors()),
            )
            .order_by(FeaturedMentor.created_at.desc(), FeaturedMentor.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _candidates(session: AsyncSession) -> list[Candidate]:
    """Every bookable mentor, with the three signals the pick weighs.

    The rating and the session count are the ones the card shows, from the
    same subqueries, so a mentor is weighted by what a mentee would see.
    """
    _, session_value = card_summary(MentorProfile.user_id)
    rows = await session.execute(
        select(
            MentorProfile.user_id,
            session_value.scalar_subquery(),
            completed_sessions(),
            # `greatest` ignores nulls: a mentor with no bio row is as fresh as
            # their mentor profile.
            func.greatest(UserProfile.updated_at, MentorProfile.updated_at),
        )
        .outerjoin(UserProfile, UserProfile.user_id == MentorProfile.user_id)
        .where(MentorProfile.user_id.in_(bookable_mentors()))
    )
    return [
        Candidate(
            id=user_id,
            session_value=float(value) if value is not None else None,
            completed_sessions=int(count or 0),
            profile_updated_at=updated,
        )
        for user_id, value, count, updated in rows
    ]


async def current_featured(session: AsyncSession, *, now: dt.datetime) -> UUID | None:
    """The mentor featured for the week `now` falls in, picking one if needed.

    `None` when nobody is bookable. Commits when it picks.
    """
    week = week_start(now)
    if (found := await _week_pick(session, week)) is not None:
        return found

    await session.execute(select(func.pg_advisory_xact_lock(PICK_LOCK)))
    # Looked for again under the lock: a request that raced this one may have
    # picked while this one waited.
    if (found := await _week_pick(session, week)) is not None:
        await session.commit()
        return found

    candidates = await _candidates(session)
    if not candidates:
        await session.rollback()
        return None

    cycle = (await session.execute(select(func.max(FeaturedMentor.cycle)))).scalar_one() or 0
    had_a_turn = set(
        (
            await session.execute(
                select(FeaturedMentor.mentor_user_id).where(FeaturedMentor.cycle == cycle)
            )
        ).scalars()
    )
    last = (
        await session.execute(
            select(FeaturedMentor.mentor_user_id)
            .order_by(FeaturedMentor.created_at.desc(), FeaturedMentor.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    this_week = (
        await session.execute(
            select(func.count())
            .select_from(FeaturedMentor)
            .where(FeaturedMentor.week_start == week)
        )
    ).scalar_one()

    pool, new_cycle = eligible(
        {c.id for c in candidates}, featured_this_cycle=had_a_turn, last_featured=last
    )
    cycle = cycle + 1 if new_cycle or cycle == 0 else cycle
    # The week, and how many picks it already had, so a replacement does not
    # land on the same seed as the pick it replaces.
    chosen = pick(
        [c for c in candidates if c.id in pool], seed=f"{week.isoformat()}:{this_week}", now=now
    )
    session.add(FeaturedMentor(mentor_user_id=chosen, week_start=week, cycle=cycle))
    await session.commit()
    return chosen
