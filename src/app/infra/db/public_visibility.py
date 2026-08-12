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

from app.domain.enums import ApprovalStatus, ListingStatus
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.sessions import SessionType

__all__ = ["mentor_is_public", "session_type_is_live"]


def mentor_is_public() -> list[Any]:
    """The mentor may be seen by someone holding no token at all.

    **Both halves, and the pair is not redundant.** `apply_mentor_status` writes
    approval or listing and never both — that separation is deliberate, so one
    event can state one fact — and no CHECK ties them. A `pending` mentor who is
    `listed` is therefore a legal row, and gating on listing alone would publish
    an unvetted mentor to anyone who asked.

    **Three clauses, not two.** `mentor_profiles` is soft-deleted, and a soft
    delete touches nothing else on the row — a removed mentor stays `approved`
    and `listed`. Checking only the status pair publishes a mentor who has been
    deleted, which is what happened here until a pre-commit sweep asked why the
    predicate did not mention `deleted_at` when `profile_store` says it "is not
    optional here, and it was missed once".

    Returned as a list of clauses rather than one `and_(...)` so a caller can
    spread it into `.where(...)` beside its own predicates and read the whole
    condition in one place.
    """
    return [
        MentorProfile.deleted_at.is_(None),
        MentorProfile.approval_status == ApprovalStatus.APPROVED,
        MentorProfile.listing_status == ListingStatus.LISTED,
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
