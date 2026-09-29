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

**One predicate here is not about the public at all**, and that is deliberate
rather than drift. `session_type_of()` is ownership plus soft deletion — the
question an *authenticated* mentor's own list asks — and it lives here because
`session_type_is_live()` is built from it. Splitting them across two modules
would put half a rule in each, which is the shape this file exists to prevent;
keeping them adjacent is what makes the difference between the two readable in
one place. The file is therefore better described as *who may see which session
type*, with the public answer being the composed one.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, or_, select

from app.domain.enums import ApprovalStatus, ListingStatus
from app.infra.db.models.availability import AvailabilityRule, SessionTypeSchedulingWindow
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.sessions import SessionType, SessionTypeBookingConfig
from app.infra.db.models.user import User
from app.infra.db.predicates import LIVE

__all__ = [
    "bookable_mentors",
    "has_live_offering",
    "has_weekly_hours",
    "mentor_exists",
    "mentor_is_bookable",
    "mentor_is_live",
    "mentor_is_public",
    "mentor_is_published",
    "mentor_is_visible_to",
    "session_type_is_live",
    "session_type_of",
]


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
    return [*mentor_exists(), mentor_is_published()]


def mentor_exists() -> list[Any]:
    """Neither the profile nor the account is soft-deleted — see above for why both.

    **Public since the similar-mentors page stopped needing the mentor to be
    live** (owner, 2026-09-29): it resolves any mentor who exists, in any state,
    and this is the one statement of what "exists" means.
    """
    return [MentorProfile.deleted_at.is_(None), LIVE]


def mentor_is_published() -> Any:
    """Approved **and** listed — the half of `mentor_is_public` that a mentor's
    own state decides, as one expression so a `SELECT` can read it as a column
    as well as filter on it."""
    return and_(
        MentorProfile.approval_status == ApprovalStatus.APPROVED,
        MentorProfile.listing_status == ListingStatus.LISTED,
    )


def mentor_is_visible_to(viewer: UUID | None) -> list[Any]:
    """`mentor_is_public()`, widened by one person: the mentor themself.

    **The owner sees their own profile in every state** — pending, declined,
    unlisted — because a mentor preparing a profile, or wondering why nobody
    books them, needs to see what it says (product rule, 2026-09-27). Nobody
    else is widened: a stranger with a token is scoped exactly as one without.

    **Soft deletion is not waived for the owner.** A deleted profile is gone for
    everyone, and the owner reading it back would be a resurrection by URL.

    **What strangers see is `mentor_is_live()`: published and bookable**
    (settled decision #192). A mentor with no offering or no weekly hours cannot
    be booked, so their profile is not shown to anyone but them — the same rule
    that already kept them off Explore, now on the profile too.

    ``None`` is the anonymous viewer and returns `mentor_is_live()` itself, not
    an equivalent spelling, so the anonymous path cannot drift from it. Every
    caller must join `users`, for the reason `mentor_is_public` gives.
    """
    if viewer is None:
        return mentor_is_live()
    return [
        *mentor_exists(),
        or_(and_(*mentor_is_live()), User.id == viewer),
    ]


def mentor_is_live() -> list[Any]:
    """Public and bookable: what a stranger may see of a mentor's profile.

    `mentor_is_public()` plus `mentor_is_bookable()`, spread. Discovery already
    listed only these mentors; since #192 the profile, its reviews and its
    similar-mentors page answer the same set, so a mentor who cannot be booked
    is not findable by a link either. Going live needs no action: the moment
    both an offering and a weekly window exist, this is true.
    """
    return [*mentor_is_public(), *mentor_is_bookable()]


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
    return [has_live_offering(), has_weekly_hours()]


def has_live_offering() -> Any:
    """`EXISTS`: at least one active, undeleted offering with a booking config.

    Half of `mentor_is_bookable`, named so a mentor can be told which half they
    are missing (`setup_needed` on their own profile).
    """
    return (
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


def has_weekly_hours() -> Any:
    """Weekly hours the slot grid can use: the mentor's own, or a live offering's.

    The other half of `mentor_is_bookable`, and it must agree with what
    `slot_store` generates slots from: an offering with its own scheduling
    windows is bookable in *those*, whatever the mentor's general hours, so a
    mentor whose only hours are an offering's windows has slots and is live
    (#199). Windows count only on a live offering — one switched off, deleted or
    without a booking config offers nothing.
    """
    general = (
        select(AvailabilityRule.id)
        .where(
            AvailabilityRule.mentor_user_id == MentorProfile.user_id,
            AvailabilityRule.is_active.is_(True),
            AvailabilityRule.deleted_at.is_(None),
        )
        .correlate(MentorProfile)
        .exists()
    )
    dedicated = (
        select(SessionTypeSchedulingWindow.id)
        .join(SessionType, SessionType.id == SessionTypeSchedulingWindow.session_type_id)
        .join(
            SessionTypeBookingConfig,
            SessionTypeBookingConfig.session_type_id == SessionType.id,
        )
        .where(
            SessionType.mentor_user_id == MentorProfile.user_id,
            SessionType.is_active.is_(True),
            SessionType.deleted_at.is_(None),
            SessionTypeSchedulingWindow.is_active.is_(True),
            SessionTypeSchedulingWindow.deleted_at.is_(None),
        )
        .correlate(MentorProfile)
        .exists()
    )
    return or_(general, dedicated)


def session_type_of(user_id: UUID) -> list[Any]:
    """This mentor's session type, existing — whether or not it is on offer.

    **Ownership and soft deletion, and deliberately not `is_active`.** The two
    clauses here answer *does this row belong to this mentor and still exist*,
    which is a different question from *may somebody book it*. The owner-facing
    list asks the first; every public read asks the second, and gets it by
    composing this with the active check below.

    ``mentor_user_id == user_id`` is not decoration: without it a caller could
    reach any mentor's session type through any mentor's URL, and every field
    returned would describe the wrong one.

    `deleted_at IS NULL` is here rather than typed into each caller because this
    project has now missed that predicate twice — five statements on `users` with
    the fifth forgotten, then four into `profile_store` on `user_awards`. A
    predicate inside SQL is not a symbol any linter can bind, so one expression is
    the only mechanism available.
    """
    return [
        SessionType.mentor_user_id == user_id,
        SessionType.deleted_at.is_(None),
    ]


def session_type_is_live(user_id: UUID) -> list[Any]:
    """This mentor's session type, still on offer.

    `is_active` covers off, closed and hidden — settled decision #90 keeps that
    one flag rather than splitting searchability from access, because a switched
    off session type should be invisible **and** unbookable.

    **Composed rather than given an `include_inactive` flag**, which was the
    obvious way to serve the owner-facing list and is the most dangerous change
    available in that pull request. This predicate decides what is *bookable*:
    `slot_store` and `profile_writer` both spread it, so a mis-defaulted flag
    reaching either one makes a deactivated session type bookable again — silently,
    and one keyword away. A narrower predicate cannot be mis-defaulted, because it
    takes no argument that could carry the mistake.
    """
    return [*session_type_of(user_id), SessionType.is_active.is_(True)]


def bookable_mentors() -> Select[Any]:
    """Every mentor discovery could show, by user id.

    The two predicates above, joined to what they read. Shared by the
    next-free-time job, which refreshes exactly these mentors, and the featured
    pick, which chooses among them — a third spelling of the same join is the
    copy that drifts (non-negotiable #8).
    """
    return (
        select(MentorProfile.user_id)
        .join(User, User.id == MentorProfile.user_id)
        .where(*mentor_is_live())
    )
