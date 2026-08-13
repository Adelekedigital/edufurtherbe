"""What a mentor offers, to anyone who asks.

The endpoint that makes `/slots` reachable. Slots require a `session_type_id`
and, until this shipped, nothing handed one out — so the slots endpoint was
correct and unusable from a browse page.

**Two statements, deliberately.** A single query filtered by both the mentor's
visibility and the session types' liveness returns zero rows for two different
situations: a mentor nobody may see, and a visible mentor offering nothing right
now. Those are different answers — 404 and an empty page — and collapsing them
would tell a caller that a mentor who has switched everything off does not
exist. The same shape as `list_session_events`, and for the same reason.

**`meeting_venue` is resolved here, not returned raw.** Null on a config means
*inherit from the mentor* (package D21), so handing the null to a client makes
them implement the cascade — and a client that gets it wrong shows "no venue"
for a mentor who has one. Settled decision #88 will move that column onto the
config and change where the fallback comes from; until that ships, this endpoint
speaks the schema that exists.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.sessions import SessionType, SessionTypeBookingConfig
from app.infra.db.models.user import User
from app.infra.db.public_visibility import mentor_is_public, session_type_is_live

__all__ = ["list_session_types"]


def _public_mentor(user_id: UUID) -> Select[Any]:
    """Whether this mentor may be seen at all, asked on its own.

    Deliberately selects a constant: nothing about the mentor is needed, only
    whether they exist publicly, and selecting columns nobody reads invites
    somebody to start reading them.
    """
    return (
        select(literal(1))
        .select_from(MentorProfile)
        # `mentor_is_public()` names `users.deleted_at`, so the join is part of
        # the contract rather than an optimisation. See that function for why it
        # is a comparison and not a subquery.
        .join(User, User.id == MentorProfile.user_id)
        .where(MentorProfile.user_id == user_id, *mentor_is_public())
    )


def _live_session_types(user_id: UUID) -> Select[Any]:
    """This mentor's session types, as a stranger sees them.

    **`created_by`, `category` and `application_stage` are absent on purpose.**
    The first is internal attribution, null on every migrated row. The other two
    are free text with no constraint, no vocabulary and no value anywhere in the
    data — publishing them would commit a public contract to a shape nobody has
    designed, and removing a field later is breaking where adding one is not.

    Ordered by name, which is unique per mentor among live rows, so the order is
    total and stable rather than merely usually-stable.
    """
    return (
        select(
            SessionType.id,
            SessionType.name,
            SessionType.description,
            SessionTypeBookingConfig.duration_minutes,
            SessionTypeBookingConfig.min_notice_minutes,
            # Null on the config means inherit (D21). Resolved here so a client
            # never has to know the cascade exists.
            func.coalesce(
                SessionTypeBookingConfig.meeting_venue,
                MentorProfile.default_meeting_venue,
            ).label("meeting_venue"),
        )
        .select_from(SessionType)
        .join(
            SessionTypeBookingConfig,
            SessionTypeBookingConfig.session_type_id == SessionType.id,
        )
        .join(MentorProfile, MentorProfile.user_id == SessionType.mentor_user_id)
        .join(User, User.id == SessionType.mentor_user_id)
        .where(*session_type_is_live(user_id), *mentor_is_public())
        .order_by(SessionType.name)
    )


async def list_session_types(session: AsyncSession, user_id: UUID) -> list[dict[str, Any]] | None:
    """Everything this mentor currently offers, or ``None`` if they are not public.

    ``None`` becomes a 404 covering an unapproved mentor, an unlisted one, and a
    user id that is nobody — indistinguishable on purpose, because telling them
    apart says which mentors exist and what state they are in.

    An **empty list** is a different and true statement: this mentor is public
    and is offering nothing bookable at the moment.
    """
    if (await session.execute(_public_mentor(user_id))).first() is None:
        return None

    result = await session.execute(_live_session_types(user_id))
    return [dict(row) for row in result.mappings()]
