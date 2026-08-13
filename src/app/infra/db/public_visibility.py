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
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.sessions import SessionType
from app.infra.db.models.user import User

__all__ = ["mentor_is_public", "session_type_is_live"]


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

    **The user clause is an `EXISTS`, deliberately, and not a join.** Written as
    a plain comparison it would only work for callers who had already joined
    `users` — and a predicate that depends on the caller's joins is a predicate a
    caller will forget, which is the entire failure this module exists to
    prevent. As a subquery it is self-contained: correct wherever it is spread,
    joined or not, and it looks up a primary key.

    Returned as a list of clauses rather than one `and_(...)` so a caller can
    spread it into `.where(...)` beside its own predicates and read the whole
    condition in one place.
    """
    return [
        MentorProfile.deleted_at.is_(None),
        MentorProfile.approval_status == ApprovalStatus.APPROVED,
        MentorProfile.listing_status == ListingStatus.LISTED,
        select(User.id)
        .where(User.id == MentorProfile.user_id, User.deleted_at.is_(None))
        # `correlate` is not optional. Left to infer, SQLAlchemy correlates away
        # every table the enclosing query already selects from — and `slot_store`
        # joins `users` for the mentor's timezone, so both tables vanished from
        # the subquery's FROM and it raised `returned no FROM clauses due to
        # auto-correlation`. Naming `MentorProfile` says: correlate that one,
        # keep `users` here. The failure is loud, but only from the caller that
        # happens to join the same table.
        .correlate(MentorProfile)
        .exists(),
    ]


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
