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
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationError
from app.domain.enums import FeaturedSource
from app.domain.featured import Candidate, eligible, pick, schedule_problem, week_start
from app.infra.db.mentor_search_store import completed_sessions
from app.infra.db.models.mentoring import FeaturedMentor, MentorProfile
from app.infra.db.models.user import User, UserProfile
from app.infra.db.public_visibility import bookable_mentors
from app.infra.db.review_stats import card_summary

__all__ = ["current_featured", "featured_schedule", "remove_featured", "set_featured"]

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

    # Nobody bookable is answered without the lock. It is the state of a fresh
    # environment, and every anonymous request there would otherwise queue on
    # the one global lock to learn, again, that there is nobody.
    if (await session.execute(select(bookable_mentors().exists()))).scalar_one() is False:
        return None

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
    # **Last week's** mentor, not the newest row: after a mid-week replacement
    # the newest row is this week's paused pick, and a new cycle keyed on it
    # would let last week's mentor come straight back.
    last = (
        await session.execute(
            select(FeaturedMentor.mentor_user_id)
            .where(FeaturedMentor.week_start < week)
            .order_by(
                FeaturedMentor.week_start.desc(),
                FeaturedMentor.created_at.desc(),
                FeaturedMentor.id.desc(),
            )
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
        [c for c in candidates if c.id in pool],
        seed=f"{week.isoformat()}:{this_week}",
        now=now,
        most_sessions=max(c.completed_sessions for c in candidates),
    )
    session.add(FeaturedMentor(mentor_user_id=chosen, week_start=week, cycle=cycle))
    await session.commit()
    return chosen


async def _is_bookable(session: AsyncSession, mentor: UUID) -> bool:
    statement = select(bookable_mentors().where(MentorProfile.user_id == mentor).exists())
    return bool((await session.execute(statement)).scalar_one())


async def set_featured(
    session: AsyncSession, week: dt.date, mentor: UUID, admin: UUID, *, now: dt.datetime
) -> None:
    """An admin chooses `mentor` for `week` (settled decision #188). Commits.

    Raises `ValidationError` for a week outside the window or a mentor who
    cannot be booked now — a mentor who pauses later is simply skipped by the
    reader and the rotation fills the week, so the page is never empty.

    **Under the pick's own lock**, so an automatic pick and an override for the
    same week cannot interleave.

    **Every earlier row of the week goes**: a previous override is replaced, and
    an automatic pick is removed so that mentor **gets their turn back** — the
    rotation reads turns from these rows, so the bumped mentor is eligible
    again. The override itself takes the rotation's current cycle, so it
    **counts as the chosen mentor's turn**.
    """
    if (problem := schedule_problem(week, now=now)) is not None:
        raise ValidationError(problem)
    await session.execute(select(func.pg_advisory_xact_lock(PICK_LOCK)))
    if not await _is_bookable(session, mentor):
        await session.rollback()
        raise ValidationError("that mentor cannot be booked, so they cannot be featured")

    cycle = (await session.execute(select(func.max(FeaturedMentor.cycle)))).scalar_one() or 1
    await session.execute(delete(FeaturedMentor).where(FeaturedMentor.week_start == week))
    session.add(
        FeaturedMentor(
            mentor_user_id=mentor,
            week_start=week,
            cycle=cycle,
            source=FeaturedSource.ADMIN,
            chosen_by=admin,
        )
    )
    await session.commit()


async def remove_featured(session: AsyncSession, week: dt.date, *, now: dt.datetime) -> bool:
    """Withdraw an admin's choice for `week`; the rotation resumes. Commits.

    `False` when the week holds no admin choice. A past week is refused: its
    record is history, and withdrawing it would change nothing anybody saw.
    """
    if (problem := schedule_problem(week, now=now)) is not None:
        raise ValidationError(problem)
    await session.execute(select(func.pg_advisory_xact_lock(PICK_LOCK)))
    result = await session.execute(
        delete(FeaturedMentor).where(
            FeaturedMentor.week_start == week, FeaturedMentor.source == FeaturedSource.ADMIN
        )
    )
    await session.commit()
    return cast("CursorResult[Any]", result).rowcount > 0


async def featured_schedule(session: AsyncSession, *, since: dt.date) -> list[dict[str, Any]]:
    """Every week from `since`, newest first: who, chosen how, by whom, and
    whether they can still be booked — an admin's future choice who has since
    paused will be skipped, and the schedule should say so."""
    bookable = FeaturedMentor.mentor_user_id.in_(bookable_mentors())
    result = await session.execute(
        select(
            FeaturedMentor.week_start,
            FeaturedMentor.mentor_user_id.label("mentor_id"),
            User.first_name,
            User.last_name,
            User.slug,
            FeaturedMentor.source,
            FeaturedMentor.chosen_by,
            bookable.label("bookable"),
        )
        .join(User, User.id == FeaturedMentor.mentor_user_id)
        .where(FeaturedMentor.week_start >= since)
        .order_by(
            FeaturedMentor.week_start.desc(),
            FeaturedMentor.created_at.desc(),
            FeaturedMentor.id.desc(),
        )
    )
    return [dict(row) for row in result.mappings()]
