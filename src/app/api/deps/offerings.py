"""`?offering=`, read once for every list that filters by service offering.

Explore (`GET /mentors`) and a mentor's reviews (`GET /mentors/{handle}/reviews`)
take the same slugs with the same rules (decisions 174 and 282), so the item
type, its bounds and the check live here rather than in either area's module.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import StringConstraints
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.common import StorableText
from app.core.errors import ValidationError
from app.infra.db.offerings import live_offering_slugs

#: An input bound, not the taxonomy's size: #53 closes the list at six, and a
#: second copy of that number here would be the one that drifts.
MAX_OFFERING_FILTERS = 10

#: An input bound on one offering slug. The column is unbounded `text`; the six
#: platform-authored slugs are all far shorter, and this only keeps a 422's echo
#: of what the caller sent short.
MAX_SLUG_LENGTH = 60

#: `?offering=` as every list that filters by it receives it: Explore and a
#: mentor's reviews (decision 282). Each endpoint wraps it in its own `Query`,
#: which holds only the description and `MAX_OFFERING_FILTERS`.
#:
#: `StorableText` per item, not on the list: a NUL in a slug reached Postgres
#: and was a 500 from one anonymous request — #97's fourth parameter. The
#: length bound keeps what a 422 echoes back short.
OfferingSlugs = (
    list[Annotated[str, StringConstraints(max_length=MAX_SLUG_LENGTH), StorableText]] | None
)


async def live_slugs(session: AsyncSession, offering: list[str] | None) -> list[str]:
    """`?offering=` normalised and checked: the slugs to filter by, in order, once each.

    The one reading of the parameter for every list that takes it (decision
    174, widened by 282). A value that empties is no filter, as a blank `q` is
    no search. An unknown slug is **refused rather than matching nothing**: a
    typo would otherwise render as an empty list and read as a supply problem.
    Retired counts as unknown, for the reason `offerings.live_offering()` gives.
    """
    slugs = list(dict.fromkeys(s for s in (v.strip() for v in offering or ()) if s))
    unknown = set(slugs) - await live_offering_slugs(session, slugs)
    if unknown:
        raise ValidationError(f"unknown offering: {', '.join(sorted(unknown))}")
    return slugs
