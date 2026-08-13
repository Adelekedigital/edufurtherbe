"""What kind of help a mentor offers — the taxonomy, not their bookable products.

**A service offering is not a session type.** This is the closed six-row
vocabulary from settled decision #53 — "Document Review", "Interview Prep" — and
it is the axis matching joins on: `mentee_goal_needs` and
`mentor_service_offerings` both point at it, and collapsing the legacy
mixed-depth list to those six parents is what makes a mentee's need and a
mentor's offer the same row. A *session type* is one mentor's own bookable
product with a duration and a price. Both are in the domain vocabulary because
the two words are close enough to be swapped by accident.

Extracted here because two profiles read it: the mentor's own, and the public
one. The query was previously inline in `profile_store` and copying it into the
public store would have been one rule in two places — the defect this repository
has now paid for four times, twice in a visibility predicate.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.db.models.mentoring import MentorServiceOffering, ServiceOffering

__all__ = ["list_service_offerings"]


async def list_service_offerings(session: AsyncSession, user_id: UUID) -> list[dict[str, Any]]:
    """The offerings this mentor has claimed, in the order the platform ranks them.

    **`is_active` is filtered, which the inline version did not do.** No row is
    inactive today — all six ship active from the M2 lookups migration and #53
    closes the list to users — so this changes nothing observable now. It matters
    the day the platform retires one: a retired offering should stop appearing on
    a mentor's public profile *and* on their own, and having the filter in one
    place means it cannot be added to half of them.

    `sort_order` rather than name: the platform decides the order these read in,
    and alphabetical would put "Document Review" above "Test Preparation" for no
    reason anybody chose.
    """
    result = await session.execute(
        select(ServiceOffering.slug, ServiceOffering.display_name)
        .select_from(MentorServiceOffering)
        .join(ServiceOffering, ServiceOffering.id == MentorServiceOffering.service_offering_id)
        .where(
            MentorServiceOffering.mentor_user_id == user_id,
            ServiceOffering.is_active.is_(True),
        )
        .order_by(ServiceOffering.sort_order)
    )
    return [dict(row) for row in result.mappings()]
