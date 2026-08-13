"""A mentor's profile, as somebody holding no token sees it.

**D20's three-clause rule becomes one here.** The package says a profile renders
if the mentor is listed, *or* the viewer has a session with them, *or* the viewer
is an admin. The middle clause is dropped by product decision: a mentee with a
session sees *that session*, not the mentor's profile, and the sessions endpoints
already carry the names they need. The admin clause is dropped because admins
read the owner-facing endpoint, which names whose records they are reviewing.
What remains is `mentor_is_public()` — the same predicate `/slots` and
`/session-types` scope by, which is why this module writes none of its own.

**Reachable by id or by slug.** `users.slug` is the legacy public profile handle
(settled decision #28), carried specifically so live profile links keep working.
Both arrive in one path segment and one statement resolves either, because a
second lookup path is where a visibility clause goes missing — every bug found in
this milestone's public endpoints has been exactly that shape.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.reference import Country
from app.infra.db.models.user import User, UserProfile
from app.infra.db.public_visibility import mentor_is_public

__all__ = ["get_public_mentor"]

_STUDY_COUNTRY = Country.__table__.alias("study_country")
_ORIGIN_COUNTRY = Country.__table__.alias("origin_country")
_CURRENT_COUNTRY = Country.__table__.alias("current_country")


def _by_handle(handle: str) -> Any:
    """Match the path segment against an id or a slug, whichever it is.

    **Parsed rather than `OR`-ed.** ``id = :h OR slug = :h`` would ask the planner
    to combine a primary key with a partial unique index for a value that can only
    ever match one of them, and the shape most likely to use neither well. Parsing
    first means each request runs the lookup it actually needs.

    The concern that argues for `OR` — two paths, one forgotten guard — is
    answered without it: this returns a *predicate*, not a query, so there is one
    statement, one spread of `mentor_is_public()`, and no branch that can carry a
    different set of clauses.

    A slug that happens to be a valid UUID string would be unreachable, since the
    parse wins. `^[a-z0-9-]+$` permits one, so that is a real if unlikely shape —
    there is no slug write path yet, and when one arrives it should refuse
    UUID-shaped slugs rather than this guessing.
    """
    try:
        return User.id == UUID(handle)
    except ValueError:
        return User.slug == handle


def _public_profile(handle: str) -> Select[Any]:
    """Everything the public may read about one mentor, in a single statement.

    The three country columns are **resolved to names here**, not returned as
    foreign keys. Handing a client a `countries.id` would reproduce exactly the
    gap the party identity change closed one pull request ago: a response that is
    correct and unusable without a second call the API does not offer.

    `user_profiles` is outer-joined because a mentor who never wrote a bio has no
    row, and an inner join would 404 them — indistinguishable from "not listed",
    and debugged in the authorization code where the defect would not be.
    """
    return (
        select(
            User.id.label("user_id"),
            User.slug,
            User.first_name,
            User.last_name,
            User.timezone,
            MentorProfile.headline,
            MentorProfile.years_of_experience,
            MentorProfile.primary_study_program,
            UserProfile.about_me,
            UserProfile.avatar_url,
            UserProfile.banner_url,
            UserProfile.social_linkedin,
            UserProfile.social_twitter,
            UserProfile.social_youtube,
            _STUDY_COUNTRY.c.display_name.label("primary_study_country"),
            _ORIGIN_COUNTRY.c.display_name.label("origin_country"),
            _CURRENT_COUNTRY.c.display_name.label("current_country"),
        )
        .select_from(User)
        .join(MentorProfile, MentorProfile.user_id == User.id)
        .outerjoin(UserProfile, UserProfile.user_id == User.id)
        .outerjoin(_STUDY_COUNTRY, _STUDY_COUNTRY.c.id == MentorProfile.primary_study_country_id)
        .outerjoin(_ORIGIN_COUNTRY, _ORIGIN_COUNTRY.c.id == UserProfile.origin_country_id)
        .outerjoin(_CURRENT_COUNTRY, _CURRENT_COUNTRY.c.id == UserProfile.current_country_id)
        .where(_by_handle(handle), *mentor_is_public())
    )


async def get_public_mentor(session: AsyncSession, handle: str) -> dict[str, Any] | None:
    """One publicly visible mentor, or ``None`` if there is no such thing.

    ``None`` covers every reason at once — no such user, not a mentor, unapproved,
    unlisted, a soft-deleted profile, a soft-deleted user, and a handle that is
    nobody. Telling them apart would say which mentors exist and what state they
    are in, which is the thing a public endpoint most easily gives away.
    """
    row = (await session.execute(_public_profile(handle))).mappings().first()
    return dict(row) if row else None
