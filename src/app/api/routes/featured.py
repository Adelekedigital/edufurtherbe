"""The week's featured mentor — one card above the explore list.

**Its own path, not `/mentors/featured`.** Mentor slugs are any `[a-z0-9-]+` and
nothing reserves `featured`, so that path would shadow a real mentor's profile.
And not a field on `/mentors`: the pick ignores the list's filters and search,
is the same for every viewer, and changes weekly — so it caches separately from
a list that will vary per signed-in viewer.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import FeaturedMentorDep
from app.api.schemas.mentors import FeaturedMentorRead

router = APIRouter(prefix="/api/v1", tags=["public"])


@router.get(
    "/featured-mentor",
    response_model=FeaturedMentorRead | None,
    summary="This week's featured mentor",
    description=(
        "One bookable mentor, chosen for the week, as a discovery card plus "
        "their `about_me`.\n\n"
        "**Public.** No token.\n\n"
        "**`null` when nobody is featured** — no mentor is bookable — rather "
        "than a `204`, so a client reads one shape.\n\n"
        "**Chosen automatically**, once a week (Monday, UTC): a random pick "
        "weighted by rating, then completed sessions, then how recently the "
        "profile changed, and nobody twice until every bookable mentor has had "
        "a turn. The pick holds for the week; if the mentor stops being "
        "bookable, a replacement is chosen from the same rotation."
    ),
)
async def read_featured_mentor(featured: FeaturedMentorDep) -> FeaturedMentorRead | None:
    return None if featured is None else FeaturedMentorRead.from_featured(featured)
