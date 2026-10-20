"""The countries a mentor can guide mentees towards (decision 276).

**One reader for both profiles.** The owner's read (and `/me`) and the public
profile call `guide_countries_of`, so the two cannot order or shape the list
differently. Visibility is the caller's: the public dependency only reaches this
for a mentor `mentor_is_visible_to` already let through.

**The writer replaces the whole list**, delete then insert, because the request
carries it whole and `position` is unique per mentor: rewriting in place would
meet that constraint mid-way through a reorder. The caller holds `lock_mentor`,
so two replacements of one list run one after the other.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.db.models.mentoring import MentorGuideCountry
from app.infra.db.models.reference import Country

__all__ = ["guide_countries_of", "replace_guide_countries"]


async def guide_countries_of(session: AsyncSession, mentor_user_id: UUID) -> list[dict[str, Any]]:
    """One mentor's guide countries, in their order: `id`, `code`, `display_name`."""
    rows = await session.execute(
        select(Country.id, Country.code, Country.display_name)
        .select_from(MentorGuideCountry)
        .join(Country, Country.id == MentorGuideCountry.country_id)
        .where(MentorGuideCountry.mentor_user_id == mentor_user_id)
        .order_by(MentorGuideCountry.position)
    )
    return [dict(row) for row in rows.mappings()]


async def replace_guide_countries(
    session: AsyncSession, mentor_user_id: UUID, country_ids: Sequence[UUID]
) -> None:
    """Replace the list with `country_ids`, position = index; `[]` clears it."""
    await session.execute(
        delete(MentorGuideCountry).where(MentorGuideCountry.mentor_user_id == mentor_user_id)
    )
    if country_ids:
        await session.execute(
            insert(MentorGuideCountry),
            [
                {"mentor_user_id": mentor_user_id, "country_id": country_id, "position": index}
                for index, country_id in enumerate(country_ids)
            ],
        )
