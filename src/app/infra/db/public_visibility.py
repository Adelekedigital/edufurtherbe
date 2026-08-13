"""What the public may see, written once.

Two endpoints now answer questions about a mentor without a viewer to scope to —
their session types and their bookable slots — and both stand or fall on the
same rule. A second copy of it would be non-negotiable #8 in its most dangerous
form: not a duplicated constant but a duplicated **control**, where each copy
makes the other untestable and a reader trusts whichever the comment points at.
This repository has already shipped that shape once, in `list_session_events`,
and a mutation batch is what found it.

So the predicates live here and nowhere else, and a mutation to either of them
turns both endpoints' tests red.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.domain.enums import ApprovalStatus, ListingStatus
from app.infra.db.models.availability import AvailabilityRule
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.sessions import SessionType, SessionTypeBookingConfig
from app.infra.db.models.user import User

__all__ = ["mentor_is_bookable", "mentor_is_public", "session_type_is_live"]


def mentor_is_public() -> list[Any]:
    """The mentor may be seen by someone holding no token at all.

    **Both halves, and the pair is not redundant.** `apply_mentor_status` writes
    approval or listing and never both — that separation is deliberate, so one
    event can state one fact — and no CHECK ties them. A `pending` mentor who is
    `listed` is therefore a legal row, and gating on listing alone would publish
    an unvetted mentor to anyone who asked.

    **Two soft deletes, not one, and they are on different tables.** A mentor is
    a `users` row *and* a `mentor_profiles` row, each carrying its own
    `deleted_at`, and nothing ties the pair together — deleting one leaves the
    other untouched and still `approved` and `listed`. Both were found the same
    way, one sweep apart: the profile's by asking what else was on that table,
    the user's by asking the same question about the table beside it.

    **The user clause is a plain comparison, and every caller must join `users`.**
    It was an `EXISTS` first, so that a caller who forgot the join could not
    silently lose the clause — sound reasoning, and incomplete. A correlated
    subquery hides `deleted_at IS NULL` from the planner, and `users` carries
    **partial** indexes predicated on exactly that: `ix_users_slug_live` is unique
    on `slug WHERE deleted_at IS NULL AND slug IS NOT NULL`. With the EXISTS form
    the planner cannot prove the query only wants live rows, so it cannot use the
    index — measured at 20,000 mentors, a slug lookup was a sequential scan
    discarding 19,999 rows, while the same lookup by id rode the primary key.

    So the clause is direct and the join is required. Forgetting it does not lose
    the clause quietly: referencing `User` without joining it produces a cartesian
    product, which SQLAlchemy warns about and which fails any test asserting a row
    count. Loud enough, and it buys back every index on `users`.

    Returned as a list of clauses rather than one `and_(...)` so a caller can
    spread it into `.where(...)` beside its own predicates and read the whole
    condition in one place.
    """
    return [
        MentorProfile.deleted_at.is_(None),
        MentorProfile.approval_status == ApprovalStatus.APPROVED,
        MentorProfile.listing_status == ListingStatus.LISTED,
        User.deleted_at.is_(None),
    ]


def mentor_is_bookable() -> list[Any]:
    """The mentor is **set up** to be booked — never that they are free.

    Two `EXISTS` clauses: at least one live session type that has a booking
    config, and at least one live availability rule. Both are about setup, and
    the distinction from *availability* is the whole point of this predicate.

    **They fail differently, which is why both are here.** Without a config
    `/slots` returns **404** — there is no duration, so there is no slot, and a
    search result linking to a 404 is worse than no result. Without an
    availability rule `/slots` returns **200 with an empty page**, which is a
    valid state — but rules are *recurring weekly*, so having none does not mean
    "nothing free this week", it means no slot will ever be generated until the
    mentor acts.

    **Service offerings are deliberately not required.** A mentor who has claimed
    none is genuinely bookable; they simply match no service filter and show an
    empty taxonomy on their card. Hiding a working mentor over a missing tag
    would be the wrong trade.

    **`EXISTS`, not joins.** A mentor with three session types and two rules would
    otherwise appear six times — and on a keyset-paged list a duplicate does not
    merely repeat a card, it consumes the page limit and shifts the cursor, so
    rows are lost at the boundary rather than seen twice.

    Written as two clauses rather than one so the exclusions stay separable: D20
    describes a dashboard stat counting the searches a mentor was filtered out
    of, and "you have no hours" and "you have no offering" are different things
    to tell them.
    """
    live_type = (
        select(SessionType.id)
        .join(
            SessionTypeBookingConfig,
            SessionTypeBookingConfig.session_type_id == SessionType.id,
        )
        .where(
            SessionType.mentor_user_id == MentorProfile.user_id,
            SessionType.is_active.is_(True),
            SessionType.deleted_at.is_(None),
        )
        .correlate(MentorProfile)
        .exists()
    )
    live_hours = (
        select(AvailabilityRule.id)
        .where(
            AvailabilityRule.mentor_user_id == MentorProfile.user_id,
            AvailabilityRule.is_active.is_(True),
            AvailabilityRule.deleted_at.is_(None),
        )
        .correlate(MentorProfile)
        .exists()
    )
    return [live_type, live_hours]


def session_type_is_live(user_id: UUID) -> list[Any]:
    """This mentor's session type, still on offer.

    ``mentor_user_id == user_id`` is not decoration: without it a caller could
    reach any mentor's session type through any mentor's URL, and every field
    returned would describe the wrong one.

    `is_active` covers off, closed and hidden — settled decision #90 keeps that
    one flag rather than splitting searchability from access, because a switched
    off session type should be invisible **and** unbookable.
    """
    return [
        SessionType.mentor_user_id == user_id,
        SessionType.is_active.is_(True),
        SessionType.deleted_at.is_(None),
    ]
