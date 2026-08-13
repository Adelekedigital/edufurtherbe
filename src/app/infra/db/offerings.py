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

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.db.models.mentoring import MentorServiceOffering, ServiceOffering

__all__ = ["offerings_for"]


async def offerings_for(
    session: AsyncSession, user_ids: Sequence[UUID]
) -> dict[UUID, list[dict[str, Any]]]:
    """Offerings for several mentors at once, keyed by mentor.

    **Plural because the list endpoint made it N+1.** A profile reads one
    mentor's offerings; discovery reads twenty. Written per-mentor it was twenty
    round trips per page, and the obvious fix — a second batched function beside
    the single one — is two queries of one rule, which is how the `is_active`
    filter ends up on one of them. So there is one query, and the profile passes
    a list of one.

    Mentors with no offerings are **absent from the mapping** rather than present
    with an empty list. The caller supplies the default, which keeps this function
    from deciding what a missing row means.

    **`is_active` is filtered, which the inline version this replaced did not
    do.** No row is inactive today — all six ship active from the M2 lookups
    migration and #53 closes the list to users — so it changes nothing
    observable now. It matters the day the platform retires one: a retired
    offering should stop appearing on a mentor's public profile *and* on their
    own, and one query means it cannot be filtered in half the places.

    `sort_order` rather than name: the platform decides how these read, and
    alphabetical would put "Document Review" above "Test Preparation" for no
    reason anybody chose.
    """
    if not user_ids:
        return {}

    result = await session.execute(
        select(
            MentorServiceOffering.mentor_user_id,
            ServiceOffering.slug,
            ServiceOffering.display_name,
        )
        .select_from(MentorServiceOffering)
        .join(ServiceOffering, ServiceOffering.id == MentorServiceOffering.service_offering_id)
        .where(
            MentorServiceOffering.mentor_user_id.in_(user_ids),
            ServiceOffering.is_active.is_(True),
        )
        .order_by(ServiceOffering.sort_order)
    )

    grouped: dict[UUID, list[dict[str, Any]]] = {}
    for row in result.mappings():
        grouped.setdefault(row["mentor_user_id"], []).append(
            {"slug": row["slug"], "display_name": row["display_name"]}
        )
    return grouped
