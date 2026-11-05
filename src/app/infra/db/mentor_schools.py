"""The schools mentors you can book come from, most mentors first (decision 307).

One statement. A mentor's school is the institution their card shows
(`top_qualification`), so the strip and the card cannot name different schools
for one mentor. Counted mentors are exactly `bookable_mentors()`: approved,
listed and taking bookings.

**Only catalogue rows a user may be offered** (`catalogue_store.VISIBLE`:
approved, not merged). A school the mentor typed and nothing matched has no
`institution_id`, and a `pending_review` row is a user's uncurated text; both
leave the mentor out rather than counting another entry of theirs.

**Grouped by name**, because the name is all the strip shows: two catalogue
rows sharing a name in different countries (decision 61) are one entry with
both schools' mentors, rather than two identical entries.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.db.catalogue_store import VISIBLE
from app.infra.db.models.education import Institution
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.public_visibility import bookable_mentors
from app.infra.db.qualifications import top_qualification

__all__ = ["mentor_schools"]


async def mentor_schools(session: AsyncSession, *, limit: int) -> list[dict[str, Any]]:
    """Up to ``limit`` schools as ``{name, mentor_count}``, most mentors first,
    ties by name."""
    school = top_qualification(MentorProfile.user_id, name="strip_school")
    mentors = func.count(MentorProfile.user_id.distinct()).label("mentor_count")
    statement = (
        bookable_mentors()
        .with_only_columns(Institution.name, mentors)
        .join(school, true())
        .join(Institution, Institution.id == school.c.institution_id)
        .where(VISIBLE, func.btrim(Institution.name) != "")
        .group_by(Institution.name)
        .order_by(mentors.desc(), Institution.name)
        .limit(limit)
    )
    return [dict(row) for row in (await session.execute(statement)).mappings()]
