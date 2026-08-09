"""The two review queues, and the actions that drain them.

**This module deliberately reads and writes other people's rows.** Every other
store in this package is scoped to one user; here the control is the caller's
grant, resolved in `api.deps.require_admin` before any of these functions runs.
Nothing below re-checks it, and nothing below may be called with an unchecked
caller.

THE PENDING QUEUE IS RANKED BY USE, AND THE QUERY WAS WRITTEN DOWN FIRST
========================================================================
The `institutions` model docstring carries this query verbatim, because
`usage_count` was dropped for being a stored counter nothing maintained — zero
on every row forever, with an index sorting a constant. The ranking the queue
actually wants is computed:

    eight users on one spelling  -> approve it
    one user plus a typo         -> merge it

Deciding a mentor's status lives in ``mentor_status_store`` rather than here:
both an admin and the mentor themselves write those transitions, so a module
named for the actor would have one importing the other's.

MERGING REPOINTS; ``merged_into_id`` IS AN AUDIT TRAIL
======================================================
Settled decision 65. The losing row's references move to the winner **in the
same transaction** that marks it merged, so the chain is collapsed at merge time
and depth is structurally zero. Resolving `merged_into_id` at read time would
put that rule into every query touching institutions — which is
non-negotiable #8, and this project has already shipped `deleted_at IS NULL`
retyped into five statements with the fifth missed.

A wrong merge stays recoverable because `education_entries.school_name_raw` is
never overwritten.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.domain.enums import ApprovalStatus, LookupStatus
from app.infra.db.models.education import EducationEntry, Institution
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.user import User


def _rowcount(result: Any) -> int:
    return cast("CursorResult[Any]", result).rowcount


async def pending_institutions(session: AsyncSession, *, limit: int) -> list[dict[str, Any]]:
    """Institutions awaiting review, most-referenced first.

    An outer join, so a row nobody references yet still appears — it is the
    common case for something typed a minute ago, and a queue that hides new
    entries until somebody else picks the same school is not a queue.
    """
    uses = func.count(EducationEntry.id).label("uses")
    statement = (
        select(
            Institution.id,
            Institution.name,
            Institution.created_at,
            Institution.created_by,
            uses,
        )
        .outerjoin(
            EducationEntry,
            and_(
                EducationEntry.institution_id == Institution.id,
                EducationEntry.deleted_at.is_(None),
            ),
        )
        .where(Institution.status == LookupStatus.PENDING_REVIEW)
        .group_by(Institution.id, Institution.name, Institution.created_at, Institution.created_by)
        .order_by(uses.desc(), Institution.created_at)
        .limit(limit)
    )
    return [dict(row) for row in (await session.execute(statement)).mappings()]


async def approve_institution(session: AsyncSession, institution_id: UUID) -> bool:
    """Make it selectable. Idempotent by construction — approving an approved
    row changes nothing and reports no change rather than failing."""
    result = await session.execute(
        update(Institution)
        .where(
            Institution.id == institution_id,
            Institution.status == LookupStatus.PENDING_REVIEW,
        )
        .values(status=LookupStatus.APPROVED)
    )
    return _rowcount(result) > 0


async def merge_institution(session: AsyncSession, *, losing_id: UUID, winning_id: UUID) -> int:
    """Point the losing row's education entries at the winner, then retire it.

    Returns how many entries moved. The caller commits: the repoint and the
    retirement are one transaction, or a half-applied merge leaves entries
    pointing at a row marked merged, which every read then has to reason about.
    """
    if losing_id == winning_id:
        raise ConflictError("an institution cannot be merged into itself")

    winner = (
        await session.execute(
            select(Institution.id, Institution.merged_into_id, Institution.status).where(
                Institution.id == winning_id
            )
        )
    ).first()
    if winner is None:
        raise ConflictError("no such institution to merge into")
    if winner.merged_into_id is not None:
        # Otherwise the chain grows: entries would point at a row that is itself
        # retired, and the next read would have to follow two hops. Collapsing
        # at merge time is the whole reason `merged_into_id` is only an audit
        # trail.
        raise ConflictError("the target has itself been merged; merge into its target instead")

    moved = await session.execute(
        update(EducationEntry)
        .where(EducationEntry.institution_id == losing_id)
        .values(institution_id=winning_id)
    )
    await session.execute(
        update(Institution)
        .where(Institution.id == losing_id)
        .values(status=LookupStatus.MERGED, merged_into_id=winning_id)
    )
    return _rowcount(moved)


async def pending_mentors(session: AsyncSession, *, limit: int) -> list[dict[str, Any]]:
    """Applications awaiting a decision, oldest first — a queue, not a stack."""
    statement = (
        select(
            MentorProfile.user_id,
            MentorProfile.headline,
            MentorProfile.years_of_experience,
            MentorProfile.created_at,
            User.first_name,
            User.last_name,
            User.email,
        )
        .join(User, User.id == MentorProfile.user_id)
        .where(
            MentorProfile.approval_status == ApprovalStatus.PENDING,
            MentorProfile.deleted_at.is_(None),
            User.deleted_at.is_(None),
        )
        .order_by(MentorProfile.created_at)
        .limit(limit)
    )
    return [dict(row) for row in (await session.execute(statement)).mappings()]
